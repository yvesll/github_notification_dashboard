from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import logging
import re
import threading
from typing import Any

import bleach
import markdown

from .ai_client import AIClient, AIClientError
from .config import DashboardConfig
from .github_api import GitHubAPI, GitHubAPIError
from .storage import Storage, utc_now


logger = logging.getLogger(__name__)
SYNC_WORKERS = 4
ARCHIVED_REFRESH_TTL = timedelta(minutes=10)

MARKDOWN_TAGS = set(bleach.sanitizer.ALLOWED_TAGS).union(
    {
        "p",
        "pre",
        "code",
        "span",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "br",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
)
MARKDOWN_ATTRIBUTES = {
    "a": ["href", "title", "rel"],
    "code": ["class"],
    "span": ["class"],
}


class DashboardService:
    def __init__(
        self,
        config: DashboardConfig,
        storage: Storage,
        github_api: GitHubAPI,
        ai_client: AIClient,
    ) -> None:
        self.config = config
        self.storage = storage
        self.github_api = github_api
        self.ai_client = ai_client
        self.sync_lock = threading.Lock()

    def bootstrap(self) -> None:
        self.storage.initialize()

    def list_notifications(self, filters: dict[str, str] | None = None) -> dict[str, Any]:
        filters = filters or {}
        active_filters = {
            "view": filters.get("view") or self.config.defaults.view,
            "status": filters.get("status") or self.config.defaults.status,
            "repository": filters.get("repository") or self.config.defaults.repository,
            "label_include": self._normalize_label_filter(
                filters.get("label_include"),
                self.config.defaults.label_include,
            ),
            "label_exclude": self._normalize_label_filter(
                filters.get("label_exclude"),
                self.config.defaults.label_exclude,
            ),
            "reason": filters.get("reason") or self.config.defaults.reason,
            "checks": filters.get("checks") or self.config.defaults.checks,
            "review": filters.get("review") or self.config.defaults.review,
            "keyword": (filters.get("keyword") or self.config.defaults.keyword).strip(),
        }

        last_sync_at = self.storage.get_metadata("last_sync_at")
        storage_filters = {
            key: value
            for key, value in active_filters.items()
            if key not in {"label_include", "label_exclude"}
        }
        items = self.storage.list_notifications(**storage_filters)
        if not items and self.github_api.is_configured() and not last_sync_at:
            self.sync_notifications(source="initial-load")
            items = self.storage.list_notifications(**storage_filters)

        if active_filters["view"] != "inbox" and (
            active_filters["checks"] != "all" or active_filters["review"] != "all"
        ):
            self._refresh_archived_metadata_if_stale()
            items = self.storage.list_notifications(**storage_filters)

        label_facets = self._label_facets(items, active_filters["repository"])
        if active_filters["label_include"]:
            items = [
                item
                for item in items
                if any(label.get("name") in active_filters["label_include"] for label in item.get("labels", []))
            ]
        if active_filters["label_exclude"]:
            items = [
                item
                for item in items
                if all(label.get("name") not in active_filters["label_exclude"] for label in item.get("labels", []))
            ]

        return {
            "items": [self._serialize_list_item(item) for item in items],
            "filters": {
                "view": active_filters["view"],
                "status": active_filters["status"],
                "repository": active_filters["repository"],
                "label_include": active_filters["label_include"],
                "label_exclude": active_filters["label_exclude"],
                "reason": active_filters["reason"],
                "checks": active_filters["checks"],
                "review": active_filters["review"],
                "keyword": active_filters["keyword"],
                **self.storage.get_facets(),
                "labels": label_facets,
            },
            "counts": self.storage.get_counts(),
            "meta": self._meta(),
        }

    def _normalize_label_filter(
        self,
        value: object,
        default: object,
    ) -> list[str]:
        if value is None:
            return self._coerce_label_values(default)
        return self._coerce_label_values(value)

    def _coerce_label_values(self, value: object) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or stripped.lower() == "all":
                return []
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return []

    def sync_notifications(self, source: str = "manual") -> dict[str, Any]:
        with self.sync_lock:
            started_at = utc_now()
            if not self.github_api.is_configured():
                self.storage.set_metadata("last_sync_at", started_at)
                self.storage.set_metadata("last_sync_source", f"{source}:skipped")
                self.storage.set_metadata("last_sync_complete", "true")
                return {
                    "synced": 0,
                    "source": source,
                    "started_at": started_at,
                    "warning": "GitHub token is not configured",
                }

            try:
                sync_payload = self.github_api.fetch_notifications(include_read=False)
            except GitHubAPIError as exc:
                if self.storage.get_counts()["all"] > 0:
                    logger.warning("Refresh failed, keeping cached notifications: %s", exc)
                    self.storage.set_metadata("last_sync_source", f"{source}:cached")
                    return {
                        "synced": 0,
                        "source": source,
                        "started_at": started_at,
                        "warning": f"GitHub refresh timed out, showing cached notifications instead: {exc}",
                    }
                raise

            notifications = sync_payload["items"]
            sync_complete = bool(sync_payload["complete"])
            existing_records = self.storage.get_notifications_by_thread_ids(
                [str(notification["id"]) for notification in notifications]
            )
            workers = min(SYNC_WORKERS, max(len(notifications), 1))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                records = list(
                    executor.map(
                        lambda notification: self._build_notification_record(
                            notification,
                            started_at,
                            existing_records.get(str(notification["id"])),
                        ),
                        notifications,
                    )
                )

            self.storage.upsert_notifications(records)
            if sync_complete:
                self.storage.reconcile_inbox_threads([record["thread_id"] for record in records])
            self.storage.set_metadata("last_sync_at", started_at)
            self.storage.set_metadata("last_sync_source", source)
            self.storage.set_metadata("last_sync_complete", "true" if sync_complete else "false")
            return {
                "synced": len(records),
                "source": source,
                "started_at": started_at,
                **(
                    {
                        "warning": (
                            "GitHub returned more notifications than max_pages allows. "
                            "Increase github.max_pages if Done/All looks incomplete."
                        )
                    }
                    if not sync_complete
                    else {}
                ),
            }

    def get_notification_detail(self, thread_id: str) -> dict[str, Any]:
        notification = self.storage.get_notification(thread_id)
        if not notification:
            raise KeyError(f"Notification {thread_id} not found")

        cached = self.storage.get_detail_cache(thread_id, notification["updated_at"])
        if cached:
            return {**cached, "cache_hit": True}

        detail = self.github_api.fetch_discussion_detail(notification)
        payload = self._serialize_detail(notification, detail)
        self.storage.set_detail_cache(thread_id, notification["updated_at"], payload)
        return {**payload, "cache_hit": False}

    def toggle_done(self, thread_id: str, is_done: bool) -> dict[str, Any]:
        notification = self.storage.get_notification(thread_id)
        if not notification:
            raise KeyError(f"Notification {thread_id} not found")

        if not is_done:
            raise ValueError("GitHub Notifications API cannot restore a done thread back to Inbox")

        if notification["is_done"]:
            return self._serialize_list_item(notification)

        self.github_api.mark_done(thread_id)
        self.storage.set_in_inbox(thread_id, False)

        updated = self.storage.get_notification(thread_id)
        return self._serialize_list_item(updated) if updated else {"thread_id": thread_id, "is_done": is_done}

    def summarize_notification(self, thread_id: str) -> dict[str, Any]:
        notification = self.storage.get_notification(thread_id)
        if not notification:
            raise KeyError(f"Notification {thread_id} not found")

        cached = self.storage.get_summary_cache(thread_id, notification["updated_at"])
        if cached and cached["raw"].get("source") != "fallback":
            return {
                "thread_id": thread_id,
                "summary": cached["summary"],
                "action_items": cached["action_items"],
                "generated_at": cached["generated_at"],
                "cache_hit": True,
                "source": cached["raw"].get("source", "ai"),
                "provider_error": self._normalize_provider_error(cached["raw"].get("provider_error")),
                "sections": cached["raw"].get("sections"),
            }

        detail = self.get_notification_detail(thread_id)
        try:
            result = self.ai_client.summarize(detail, notification)
            summary_source = "ai"
            provider_error = None
        except AIClientError as exc:
            logger.warning("AI provider unavailable for thread %s: %s", thread_id, exc)
            result = self._fallback_summary(detail, notification, self._normalize_provider_error(str(exc)))
            summary_source = "fallback"
            provider_error = self._normalize_provider_error(str(exc))

        self.storage.set_summary_cache(
            thread_id,
            notification["updated_at"],
            result["summary"],
            result["action_items"],
            {
                **result["raw"],
                "source": summary_source,
                "provider_error": provider_error,
            },
        )
        return {
            "thread_id": thread_id,
            "summary": result["summary"],
            "action_items": result["action_items"],
            "generated_at": utc_now(),
            "cache_hit": False,
            "source": summary_source,
            "provider_error": provider_error,
            "sections": result["raw"].get("sections"),
        }

    def webhook_event(self, event: str, payload: bytes, signature: str | None) -> dict[str, Any]:
        if not self.github_api.verify_webhook_signature(self.config.webhook.secret, payload, signature):
            raise PermissionError("Invalid webhook signature")

        if event == "ping":
            return {"event": event, "message": "pong"}

        if event in {
            "issues",
            "issue_comment",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
        }:
            result = self.sync_notifications(source=f"webhook:{event}")
            return {"event": event, **result}

        return {"event": event, "ignored": True}

    def _meta(self) -> dict[str, Any]:
        return {
            "last_sync_at": self.storage.get_metadata("last_sync_at"),
            "last_sync_source": self.storage.get_metadata("last_sync_source"),
            "last_sync_complete": self.storage.get_metadata("last_sync_complete") != "false",
            "ai_enabled": self.ai_client.is_configured(),
            "github_configured": self.github_api.is_configured(),
            "polling_enabled": self.config.polling.enabled,
            "polling_interval_seconds": self.config.polling.interval_seconds,
        }

    def _serialize_list_item(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "thread_id": item["thread_id"],
            "repository": item["repository_full_name"],
            "repository_name": item["repository_name"],
            "type": item["subject_type"],
            "title": item["subject_title"],
            "status": item["status"],
            "reason": item["reason"],
            "updated_at": item["updated_at"],
            "last_read_at": item["last_read_at"],
            "web_url": item["web_url"],
            "checks_state": item["checks_state"],
            "review_decision": item["review_decision"],
            "labels": item["labels"],
            "is_done": item["is_done"],
            "in_inbox": item["in_inbox"],
            "unread": item["unread"],
        }

    def _serialize_detail(
        self,
        notification: dict[str, Any],
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        reviews = [
            {
                "author": review.get("user", {}).get("login"),
                "state": review.get("state", "COMMENTED"),
                "submitted_at": review.get("submitted_at"),
                "body_html": self.render_markdown(review.get("body", "")),
                "body_text": review.get("body", ""),
            }
            for review in detail.get("reviews", [])
        ]
        comments = [
            {
                "kind": "comment",
                "author": comment.get("user", {}).get("login"),
                "created_at": comment.get("created_at"),
                "body_html": self.render_markdown(comment.get("body", "")),
                "body_text": comment.get("body", ""),
            }
            for comment in detail.get("comments", [])
        ]
        review_comments = [
            {
                "kind": "review_comment",
                "author": comment.get("user", {}).get("login"),
                "created_at": comment.get("created_at"),
                "body_html": self.render_markdown(comment.get("body", "")),
                "body_text": comment.get("body", ""),
                "path": comment.get("path"),
                "line": comment.get("line") or comment.get("original_line"),
            }
            for comment in detail.get("review_comments", [])
        ]

        return {
            "thread_id": notification["thread_id"],
            "repository": notification["repository_full_name"],
            "type": notification["subject_type"],
            "title": detail.get("title") or notification["subject_title"],
            "status": detail.get("state") or notification["status"],
            "reason": notification["reason"],
            "updated_at": notification["updated_at"],
            "html_url": detail.get("html_url") or notification.get("web_url"),
            "author": detail.get("author"),
            "number": detail.get("number"),
            "body_html": self.render_markdown(detail.get("body", "")),
            "body_text": detail.get("body", ""),
            "comments": comments,
            "review_comments": review_comments,
            "reviews": reviews,
            "review_rollup": detail.get("review_rollup", []),
            "requested_reviewers": detail.get("requested_reviewers", []),
            "participants": detail.get("participants", []),
        }

    def render_markdown(self, text: str) -> str:
        html = markdown.markdown(
            text or "",
            extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        )
        cleaned = bleach.clean(
            html,
            tags=MARKDOWN_TAGS,
            attributes=MARKDOWN_ATTRIBUTES,
            protocols=["http", "https", "mailto"],
            strip=True,
        )
        return bleach.linkify(cleaned)

    def _build_notification_record(
        self,
        notification: dict[str, Any],
        started_at: str,
        existing_record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        repository = notification.get("repository", {})
        subject = notification.get("subject", {})
        updated_at = notification.get("updated_at", started_at)

        if (
            existing_record
            and existing_record.get("updated_at") == updated_at
            and self._can_reuse_cached_metadata(existing_record, subject.get("type", "Unknown"))
        ):
            return {
                "thread_id": str(notification["id"]),
                "repository_full_name": repository.get("full_name") or repository.get("name") or "unknown",
                "repository_name": repository.get("name") or repository.get("full_name") or "unknown",
                "subject_type": subject.get("type", "Unknown"),
                "subject_title": subject.get("title", "Untitled"),
                "reason": notification.get("reason", "unknown"),
                "unread": notification.get("unread", False),
                "updated_at": updated_at,
                "last_read_at": notification.get("last_read_at"),
                "status": existing_record.get("status", "unknown"),
                "subject_url": subject.get("url"),
                "web_url": existing_record.get("web_url"),
                "checks_state": existing_record.get("checks_state", "none"),
                "review_decision": existing_record.get("review_decision", "none"),
                "labels": existing_record.get("labels", []),
                "search_text": existing_record.get("search_text", ""),
                "in_inbox": True,
                "raw_json": notification,
            }

        try:
            state = self.github_api.resolve_notification_state(notification)
        except GitHubAPIError as exc:
            logger.warning(
                "Falling back to cached metadata for notification %s: %s",
                notification.get("id"),
                exc,
            )
            state = {
                "status": existing_record.get("status", "unknown") if existing_record else "unknown",
                "web_url": existing_record.get("web_url") if existing_record else repository.get("html_url"),
                "checks_state": existing_record.get("checks_state", "none") if existing_record else "none",
                "review_decision": existing_record.get("review_decision", "none") if existing_record else "none",
                "labels": existing_record.get("labels", []) if existing_record else [],
                "search_text": existing_record.get("search_text", "") if existing_record else "",
            }
        except Exception:
            logger.exception("Failed to resolve extended state for notification %s", notification.get("id"))
            state = {
                "status": "unknown",
                "web_url": repository.get("html_url"),
                "checks_state": "none",
                "review_decision": "none",
                "labels": [],
                "search_text": "",
            }

        return {
            "thread_id": str(notification["id"]),
            "repository_full_name": repository.get("full_name") or repository.get("name") or "unknown",
            "repository_name": repository.get("name") or repository.get("full_name") or "unknown",
            "subject_type": subject.get("type", "Unknown"),
            "subject_title": subject.get("title", "Untitled"),
            "reason": notification.get("reason", "unknown"),
            "unread": notification.get("unread", False),
            "updated_at": updated_at,
            "last_read_at": notification.get("last_read_at"),
            "status": state["status"],
            "subject_url": subject.get("url"),
            "web_url": state.get("web_url"),
            "checks_state": state.get("checks_state", "none"),
            "review_decision": state.get("review_decision", "none"),
            "labels": state.get("labels", []),
            "search_text": state.get("search_text", ""),
            "in_inbox": True,
            "raw_json": notification,
        }

    def _can_reuse_cached_metadata(self, existing_record: dict[str, Any], subject_type: str) -> bool:
        if subject_type == "PullRequest" and existing_record.get("checks_state") != "passed":
            return False
        labels = existing_record.get("labels", [])
        if not labels:
            return True
        return all(str(label.get("color") or "").strip() for label in labels if isinstance(label, dict))

    def _refresh_archived_metadata_if_stale(self) -> None:
        last_refresh = self.storage.get_metadata("last_archived_refresh_at")
        if last_refresh:
            try:
                last_refresh_at = datetime.fromisoformat(last_refresh)
            except ValueError:
                last_refresh_at = None
            else:
                if datetime.now(UTC) - last_refresh_at < ARCHIVED_REFRESH_TTL:
                    return

        self._refresh_archived_metadata()
        self.storage.set_metadata("last_archived_refresh_at", utc_now())

    def _refresh_archived_metadata(self) -> None:
        archived_items = self.storage.list_archived_notifications("all")
        if not archived_items:
            return

        workers = min(2, max(len(archived_items), 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            refreshed_records = list(executor.map(self._refresh_archived_record, archived_items))

        self.storage.upsert_notifications(refreshed_records)

    def _refresh_archived_record(self, item: dict[str, Any]) -> dict[str, Any]:
        synthetic_notification = {
            "id": item["thread_id"],
            "repository": {
                "full_name": item["repository_full_name"],
                "name": item["repository_name"],
                "html_url": f"https://github.com/{item['repository_full_name']}",
            },
            "subject": {
                "type": item["subject_type"],
                "title": item["subject_title"],
                "url": item["subject_url"],
            },
            "reason": item["reason"],
            "unread": False,
            "updated_at": item["updated_at"],
            "last_read_at": item["last_read_at"],
        }

        try:
            state = self.github_api.resolve_notification_state(synthetic_notification)
        except GitHubAPIError as exc:
            logger.warning("Keeping cached archived metadata for thread %s: %s", item["thread_id"], exc)
            state = {
                "status": item["status"],
                "web_url": item["web_url"],
                "checks_state": item["checks_state"],
                "review_decision": item["review_decision"],
                "labels": item["labels"],
                "search_text": item.get("search_text", ""),
            }

        return {
            "thread_id": item["thread_id"],
            "repository_full_name": item["repository_full_name"],
            "repository_name": item["repository_name"],
            "subject_type": item["subject_type"],
            "subject_title": item["subject_title"],
            "reason": item["reason"],
            "unread": False,
            "updated_at": item["updated_at"],
            "last_read_at": item["last_read_at"],
            "status": state.get("status", item["status"]),
            "subject_url": item["subject_url"],
            "web_url": state.get("web_url", item["web_url"]),
            "checks_state": state.get("checks_state", item["checks_state"]),
            "review_decision": state.get("review_decision", item["review_decision"]),
            "labels": state.get("labels", item["labels"]),
            "search_text": state.get("search_text", item.get("search_text", "")),
            "in_inbox": False,
            "raw_json": item["raw_json"],
        }

    def _label_facets(self, items: list[dict[str, Any]], repository: str) -> list[dict[str, str]]:
        if repository == "all":
            return []

        labels_by_name: dict[str, dict[str, str]] = {}
        for item in items:
            for label in item.get("labels", []):
                name = str(label.get("name") or "").strip()
                if not name:
                    continue
                existing = labels_by_name.get(name)
                if not existing or (not existing.get("color") and label.get("color")):
                    labels_by_name[name] = {
                        "name": name,
                        "color": str(label.get("color") or "").strip(),
                    }

        return sorted(labels_by_name.values(), key=lambda label: label["name"].lower())

    def _fallback_summary(
        self,
        detail: dict[str, Any],
        notification: dict[str, Any],
        provider_error: str,
    ) -> dict[str, Any]:
        review_rollup = detail.get("review_rollup", [])
        comments = [*(detail.get("comments", [])), *(detail.get("review_comments", []))]
        review_states = [review.get("state", "").lower() for review in review_rollup]
        approvals = sum(1 for state in review_states if state == "approved")
        changes_requested = sum(1 for state in review_states if state == "changes_requested")
        comment_count = len(comments)
        review_count = len(detail.get("reviews", []))
        participants = detail.get("participants", [])
        what_changed = self._summarize_change(detail)
        current_discussion = self._summarize_discussion(detail, review_rollup)
        latest_status = self._summarize_status(
            detail,
            notification,
            participants=len(participants),
            review_count=review_count,
            comment_count=comment_count,
            approvals=approvals,
            changes_requested=changes_requested,
        )

        summary_parts = [
            f"What changed: {what_changed}",
            f"Current discussion: {current_discussion}",
            f"Latest status: {latest_status}",
        ]

        action_items = self._extract_action_items(detail, review_rollup)
        if not action_items:
            if changes_requested:
                reviewers = ", ".join(
                    review["author"]
                    for review in review_rollup
                    if review.get("state", "").lower() == "changes_requested"
                )
                action_items.append(
                    f"Address requested changes{f' from {reviewers}' if reviewers else ''}."
                )
            elif detail.get("requested_reviewers"):
                action_items.append(
                    f"Wait for review from {', '.join(detail['requested_reviewers'])}."
                )
            else:
                action_items.append("No explicit action item was detected from the current discussion.")

        return {
            "summary": " ".join(summary_parts),
            "action_items": action_items[:5],
            "raw": {
                "source": "fallback",
                "provider_error": provider_error,
                "sections": {
                    "what_changed": what_changed,
                    "current_discussion": current_discussion,
                    "latest_status": latest_status,
                },
            },
        }

    def _summarize_change(self, detail: dict[str, Any]) -> str:
        title = str(detail.get("title") or "").strip()
        body = self._first_substantive_paragraph(detail.get("body_text", ""))
        normalized_title = re.sub(r"^(?:[^:]+:\s*)+", "", title).strip().lower()

        if body:
            if normalized_title and body.lower().startswith(normalized_title):
                return body
            if title and body.lower().startswith(title.lower()):
                return body
            if title and title.lower() not in body.lower():
                return f"{title}. {body}"
            return body

        return title or "The PR description does not explain the change clearly yet."

    def _summarize_discussion(
        self,
        detail: dict[str, Any],
        review_rollup: list[dict[str, Any]],
    ) -> str:
        latest_item = self._latest_discussion_item(detail)
        if latest_item:
            author = latest_item.get("author") or "someone"
            state = latest_item.get("state")
            excerpt = latest_item.get("excerpt") or ""
            if state == "review_comment":
                return f"Latest discussion from {author}: {excerpt}"
            if state and state not in {"COMMENTED", "comment"}:
                return f"{author} left a {state.lower().replace('_', ' ')} review: {excerpt}"
            return f"Latest discussion from {author}: {excerpt}"

        requested_changes = [
            review["author"]
            for review in review_rollup
            if review.get("state", "").lower() == "changes_requested"
        ]
        if requested_changes:
            return f"The latest blocking signal is a changes-requested review from {', '.join(requested_changes)}."

        return "No substantive discussion yet."

    def _summarize_status(
        self,
        detail: dict[str, Any],
        notification: dict[str, Any],
        *,
        participants: int,
        review_count: int,
        comment_count: int,
        approvals: int,
        changes_requested: int,
    ) -> str:
        parts = [
            f"This {self._display_subject_type(notification['subject_type'])} is {detail.get('status', notification['status'])}.",
            f"{participants} participants, {review_count} reviews, and {comment_count} comments so far.",
        ]

        review_bits: list[str] = []
        if approvals:
            review_bits.append(f"{approvals} approval{'s' if approvals != 1 else ''}")
        if changes_requested:
            review_bits.append(f"{changes_requested} change request{'s' if changes_requested != 1 else ''}")
        if review_bits:
            parts.append("Latest review rollup: " + ", ".join(review_bits) + ".")

        requested_reviewers = detail.get("requested_reviewers") or []
        if requested_reviewers:
            if len(requested_reviewers) > 6:
                preview = ", ".join(requested_reviewers[:4])
                parts.append(
                    f"Still waiting on {len(requested_reviewers)} requested reviewers, including {preview}."
                )
            else:
                parts.append("Waiting on review from " + ", ".join(requested_reviewers) + ".")

        return " ".join(parts)

    def _latest_excerpt(self, detail: dict[str, Any]) -> str:
        timeline = [
            *detail.get("comments", []),
            *detail.get("review_comments", []),
            *detail.get("reviews", []),
        ]
        timeline = [
            item
            for item in timeline
            if (item.get("body_text") or item.get("body") or "").strip()
        ]
        if not timeline:
            return ""

        latest = max(
            timeline,
            key=lambda item: item.get("created_at") or item.get("submitted_at") or "",
        )
        text = (latest.get("body_text") or latest.get("body") or "").strip()
        text = re.sub(r"\s+", " ", text)
        return text[:220] + ("..." if len(text) > 220 else "")

    def _latest_discussion_item(self, detail: dict[str, Any]) -> dict[str, str] | None:
        timeline: list[dict[str, str]] = []

        for review in detail.get("reviews", []):
            excerpt = self._clean_text_for_summary(review.get("body_text") or "")
            if excerpt:
                timeline.append(
                    {
                        "author": str(review.get("author") or "someone"),
                        "state": str(review.get("state") or "COMMENTED"),
                        "timestamp": str(review.get("submitted_at") or ""),
                        "excerpt": excerpt,
                    }
                )

        for comment in [*(detail.get("comments", [])), *(detail.get("review_comments", []))]:
            excerpt = self._clean_text_for_summary(comment.get("body_text") or "")
            if excerpt:
                timeline.append(
                    {
                        "author": str(comment.get("author") or "someone"),
                        "state": str(comment.get("kind") or "comment"),
                        "timestamp": str(comment.get("created_at") or ""),
                        "excerpt": excerpt,
                    }
                )

        if not timeline:
            return None

        non_bot_items = [
            item
            for item in timeline
            if not self._is_bot_author(item["author"])
        ]
        candidates = non_bot_items or timeline
        return max(candidates, key=lambda item: item["timestamp"])

    def _first_substantive_paragraph(self, text: str) -> str:
        if not text:
            return ""

        paragraphs = re.split(r"\n\s*\n", text)
        for paragraph in paragraphs:
            cleaned = self._clean_text_for_summary(paragraph)
            if len(cleaned) >= 24:
                return cleaned
        return self._clean_text_for_summary(text)

    def _clean_text_for_summary(self, text: str) -> str:
        if not text:
            return ""

        lines: list[str] = []
        for raw_line in text.splitlines():
            line = re.sub(r"^\s*>+\s?", "", raw_line)
            line = re.sub(r"^\s*#{1,6}\s*", "", line)
            line = re.sub(r"^\s*[-*+]\s+", "", line)
            line = re.sub(r"^\s*\d+\.\s+", "", line)
            line = re.sub(r"!\[.*?\]\([^)]+\)", "", line)
            line = re.sub(r"`([^`]*)`", r"\1", line)
            line = line.strip()
            if not line:
                continue
            if line.startswith("```"):
                continue
            lines.append(line)

        cleaned = " ".join(lines)
        cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", cleaned)
        cleaned = re.sub(r"https?://\S+", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:260] + ("..." if len(cleaned) > 260 else "")

    def _display_subject_type(self, subject_type: str) -> str:
        if subject_type == "PullRequest":
            return "pull request"
        return subject_type.lower()

    def _is_bot_author(self, author: str) -> bool:
        lowered = author.lower()
        return lowered.endswith("[bot]") or lowered.endswith("-bot") or lowered == "sonarqubecloud[bot]"

    def _extract_action_items(
        self,
        detail: dict[str, Any],
        review_rollup: list[dict[str, Any]],
    ) -> list[str]:
        snippets: list[str] = []
        bodies = [
            *(detail.get("comments", [])),
            *(detail.get("review_comments", [])),
            *(detail.get("reviews", [])),
        ]
        for item in bodies:
            text = (item.get("body_text") or item.get("body") or "").strip()
            if not text:
                continue
            for line in text.splitlines():
                normalized = re.sub(r"^\s*>+\s?", "", line)
                normalized = re.sub(r"\s+", " ", normalized).strip(" -*\t")
                if not normalized:
                    continue
                lowered = normalized.lower()
                if (
                    normalized.startswith("[ ]")
                    or normalized.startswith("todo")
                    or "please " in lowered
                    or "could you" in lowered
                    or "need to" in lowered
                    or "should " in lowered
                    or "must " in lowered
                ):
                    snippets.append(normalized[:180])

        deduped: list[str] = []
        seen: set[str] = set()
        for snippet in snippets:
            key = snippet.lower()
            if key not in seen:
                deduped.append(snippet)
                seen.add(key)

        if deduped:
            return deduped

        requested_changes = [
            review["author"]
            for review in review_rollup
            if review.get("state", "").lower() == "changes_requested"
        ]
        if requested_changes:
            return [f"Address requested changes from {', '.join(requested_changes)}."]

        return []

    def _normalize_provider_error(self, provider_error: str | None) -> str | None:
        if not provider_error:
            return None

        lowered = provider_error.lower()
        if "no_available_providers" in lowered or "no available providers" in lowered:
            return "Configured AI gateway currently has no available upstream providers"
        if (
            "concurrent session" in lowered
            or "rate_limit_exceeded" in lowered
            or "并发 session 超限" in lowered
            or "并发session超限" in lowered
            or "活跃 session" in lowered
        ):
            return "Configured AI gateway hit its concurrent session limit"
        return provider_error


class PollingWorker:
    def __init__(self, service: DashboardService, interval_seconds: int, run_initial_sync: bool) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self.run_initial_sync = run_initial_sync
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="gh-dashboard-poller")

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1)

    def _run_loop(self) -> None:
        if self.run_initial_sync:
            self._safe_sync("startup")

        while not self.stop_event.wait(self.interval_seconds):
            self._safe_sync("poller")

    def _safe_sync(self, source: str) -> None:
        try:
            self.service.sync_notifications(source=source)
        except GitHubAPIError:
            logger.exception("Polling sync failed via GitHub API")
        except Exception:
            logger.exception("Polling sync failed unexpectedly")
