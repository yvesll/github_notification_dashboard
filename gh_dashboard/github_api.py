#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import defaultdict
import hashlib
import hmac
import json
import threading
from typing import Any
from urllib.parse import urljoin

import requests

from .config import GitHubConfig


class GitHubAPIError(RuntimeError):
    pass


class GitHubAPI:
    def __init__(self, config: GitHubConfig) -> None:
        self.config = config
        self._thread_local = threading.local()

    def is_configured(self) -> bool:
        return bool(self.config.token)

    def fetch_notifications(self, *, include_read: bool = False) -> dict[str, Any]:
        if not self.is_configured():
            return {"items": [], "complete": True}

        notifications: list[dict[str, Any]] = []
        next_url: str | None = None
        page = 1
        complete = True

        while page <= self.config.max_pages:
            response = self._raw_request(
                "GET",
                next_url or "/notifications",
                absolute=bool(next_url),
                params=None
                if next_url
                else {
                    "all": "true" if include_read else "false",
                    "per_page": self.config.notifications_per_page,
                    "page": page,
                },
                expected=(200,),
            )
            payload = response.json()
            notifications.extend(
                item
                for item in payload
                if item.get("subject", {}).get("type") in {"PullRequest", "Issue"}
            )
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                break
            if page == self.config.max_pages:
                complete = False
                break
            page += 1

        return {
            "items": notifications,
            "complete": complete,
        }

    def fetch_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request("GET", f"/notifications/threads/{thread_id}")

    def fetch_subject(self, subject_url: str) -> dict[str, Any]:
        return self._request("GET", subject_url, absolute=True)

    def fetch_json_collection(self, url: str) -> list[dict[str, Any]]:
        if not url:
            return []

        items: list[dict[str, Any]] = []
        next_url: str | None = url
        seen = 0

        while next_url and seen < 6:
            response = self._raw_request("GET", next_url, absolute=True, expected=(200,))
            page_items = response.json()
            if isinstance(page_items, list):
                items.extend(page_items)
            next_url = response.links.get("next", {}).get("url")
            seen += 1

        return items

    def resolve_notification_state(self, notification: dict[str, Any]) -> dict[str, Any]:
        repo_html_url = notification.get("repository", {}).get("html_url")
        repository = notification.get("repository", {})
        subject = notification.get("subject", {})
        subject_url = subject.get("url")
        subject_type = subject.get("type")

        if not subject_url:
            return {
                "status": "unknown",
                "web_url": repo_html_url,
                "checks_state": "none",
                "review_decision": "none",
                "labels": [],
                "search_text": "",
            }

        payload = self.fetch_subject(subject_url)
        labels = self.extract_labels(payload)

        if subject_type == "PullRequest":
            pull_request = payload
            if "merged" not in pull_request:
                pull_request_url = payload.get("pull_request", {}).get("url")
                if pull_request_url:
                    pull_request = self.fetch_subject(pull_request_url)

            if not labels:
                issue_url = pull_request.get("issue_url") or payload.get("issue_url")
                if issue_url:
                    issue_payload = self.fetch_subject(issue_url)
                    labels = self.extract_labels(issue_payload)

            status = "merged" if pull_request.get("merged") else pull_request.get("state", "unknown")
            review_rollup = self.fetch_pull_request_review_rollup(pull_request)
            return {
                "status": status,
                "web_url": pull_request.get("html_url") or payload.get("html_url") or repo_html_url,
                "checks_state": self.fetch_pull_request_checks_state(pull_request, repository),
                "review_decision": self.review_decision(review_rollup),
                "labels": labels,
                "search_text": self.build_search_text(
                    pull_request.get("title") or payload.get("title") or "",
                    pull_request.get("body") or payload.get("body") or "",
                ),
            }

        return {
            "status": payload.get("state", "unknown"),
            "web_url": payload.get("html_url") or repo_html_url,
            "checks_state": "none",
            "review_decision": "none",
            "labels": labels,
            "search_text": self.build_search_text(
                payload.get("title") or "",
                payload.get("body") or "",
            ),
        }

    def fetch_discussion_detail(self, notification: dict[str, Any]) -> dict[str, Any]:
        thread = self.fetch_thread(str(notification["thread_id"]))
        subject_url = thread.get("subject", {}).get("url") or notification.get("subject_url")
        subject_type = notification["subject_type"]
        base_payload = self.fetch_subject(subject_url) if subject_url else {}

        if subject_type == "PullRequest":
            return self._build_pull_request_detail(notification, thread, base_payload)
        return self._build_issue_detail(notification, thread, base_payload)

    def mark_done(self, thread_id: str) -> None:
        self._request("DELETE", f"/notifications/threads/{thread_id}", expected=(204,))

    def verify_webhook_signature(self, secret: str, payload: bytes, signature_header: str | None) -> bool:
        if not secret:
            return True
        if not signature_header:
            return False

        digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        expected_signature = f"sha256={digest}"
        return hmac.compare_digest(expected_signature, signature_header)

    def summarize_review_rollup(self, reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
        latest_by_author: dict[str, dict[str, Any]] = defaultdict(dict)
        for review in reviews:
            login = review.get("user", {}).get("login") or "unknown"
            submitted_at = review.get("submitted_at") or review.get("submittedAt") or ""
            current = latest_by_author.get(login)
            if not current or submitted_at >= (current.get("submitted_at") or ""):
                latest_by_author[login] = {
                    "author": login,
                    "state": review.get("state", "COMMENTED"),
                    "submitted_at": submitted_at,
                }
        return sorted(latest_by_author.values(), key=lambda item: item["author"].lower())

    def review_decision(self, review_rollup: list[dict[str, Any]]) -> str:
        states = {str(review.get("state", "")).upper() for review in review_rollup}
        if "CHANGES_REQUESTED" in states:
            return "changes_requested"
        if "APPROVED" in states:
            return "approved"
        if states:
            return "commented"
        return "none"

    def fetch_pull_request_review_rollup(self, pull_request: dict[str, Any]) -> list[dict[str, Any]]:
        pull_request_url = pull_request.get("url", "")
        reviews = self.fetch_json_collection(f"{pull_request_url}/reviews") if pull_request_url else []
        return self.summarize_review_rollup(reviews)

    def fetch_pull_request_checks_state(
        self,
        pull_request: dict[str, Any],
        repository: dict[str, Any],
    ) -> str:
        status_state = "none"
        check_runs: list[dict[str, Any]] = []
        repository_full_name = (
            repository.get("full_name")
            or pull_request.get("head", {}).get("repo", {}).get("full_name")
            or ""
        )
        head_sha = pull_request.get("head", {}).get("sha")
        if repository_full_name and "/" in repository_full_name and head_sha:
            owner, repo = repository_full_name.split("/", 1)
            status_payload = self._request(
                "GET",
                f"/repos/{owner}/{repo}/commits/{head_sha}/status",
            )
            status_state = str(status_payload.get("state", "none")).lower()
            checks_payload = self._request(
                "GET",
                f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
                params={"per_page": 100},
            )
            check_runs = checks_payload.get("check_runs", [])

        return self._summarize_checks_state(status_state, check_runs)

    def _summarize_checks_state(
        self,
        status_state: str,
        check_runs: list[dict[str, Any]],
    ) -> str:
        pending_statuses = {"queued", "in_progress", "requested", "waiting", "pending"}
        failed_conclusions = {
            "action_required",
            "cancelled",
            "failure",
            "stale",
            "startup_failure",
            "timed_out",
        }
        passing_conclusions = {"success", "neutral", "skipped"}

        if check_runs:
            for check_run in check_runs:
                status = str(check_run.get("status", "")).lower()
                conclusion = str(check_run.get("conclusion", "")).lower()
                if status in pending_statuses or (status != "completed" and not conclusion):
                    return "other"
                if conclusion in failed_conclusions:
                    return "other"

            if all(str(check_run.get("conclusion", "")).lower() in passing_conclusions for check_run in check_runs):
                return "passed"
            return "other"

        if status_state in {"none", ""}:
            return "other"
        if status_state in {"failure", "error"}:
            return "other"
        if status_state == "success":
            return "passed"
        return "other"

    def _build_issue_detail(
        self,
        notification: dict[str, Any],
        thread: dict[str, Any],
        issue: dict[str, Any],
    ) -> dict[str, Any]:
        comments = self.fetch_json_collection(issue.get("comments_url", ""))

        return {
            "thread": thread,
            "kind": "issue",
            "number": issue.get("number"),
            "title": issue.get("title") or notification["subject_title"],
            "state": issue.get("state", notification["status"]),
            "html_url": issue.get("html_url") or notification.get("web_url"),
            "author": issue.get("user", {}).get("login"),
            "body": issue.get("body", ""),
            "comments": comments,
            "review_comments": [],
            "reviews": [],
            "review_rollup": [],
            "requested_reviewers": [],
            "participants": self._participants(issue, comments, []),
        }

    def _build_pull_request_detail(
        self,
        notification: dict[str, Any],
        thread: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        issue_payload = payload
        pull_request_payload = payload

        if "merged" not in pull_request_payload or "review_comments_url" not in pull_request_payload:
            pull_request_url = payload.get("pull_request", {}).get("url")
            if pull_request_url:
                pull_request_payload = self.fetch_subject(pull_request_url)

        if "comments_url" not in issue_payload:
            issue_url = pull_request_payload.get("issue_url")
            if issue_url:
                issue_payload = self.fetch_subject(issue_url)

        comments = self.fetch_json_collection(issue_payload.get("comments_url", ""))
        review_comments = self.fetch_json_collection(pull_request_payload.get("review_comments_url", ""))
        pull_request_url = pull_request_payload.get("url", "")
        reviews = self.fetch_json_collection(f"{pull_request_url}/reviews") if pull_request_url else []

        return {
            "thread": thread,
            "kind": "pull_request",
            "number": pull_request_payload.get("number") or issue_payload.get("number"),
            "title": pull_request_payload.get("title") or issue_payload.get("title") or notification["subject_title"],
            "state": "merged"
            if pull_request_payload.get("merged")
            else pull_request_payload.get("state", notification["status"]),
            "html_url": pull_request_payload.get("html_url") or issue_payload.get("html_url") or notification.get("web_url"),
            "author": pull_request_payload.get("user", {}).get("login") or issue_payload.get("user", {}).get("login"),
            "body": pull_request_payload.get("body") or issue_payload.get("body") or "",
            "comments": comments,
            "review_comments": review_comments,
            "reviews": reviews,
            "review_rollup": self.summarize_review_rollup(reviews),
            "requested_reviewers": [
                reviewer.get("login")
                for reviewer in pull_request_payload.get("requested_reviewers", [])
                if reviewer.get("login")
            ],
            "participants": self._participants(issue_payload, comments, review_comments),
        }

    def _participants(
        self,
        subject: dict[str, Any],
        comments: list[dict[str, Any]],
        review_comments: list[dict[str, Any]],
    ) -> list[str]:
        people = {
            item
            for item in [
                subject.get("user", {}).get("login"),
                *[comment.get("user", {}).get("login") for comment in comments],
                *[comment.get("user", {}).get("login") for comment in review_comments],
            ]
            if item
        }
        return sorted(people, key=str.lower)

    def extract_labels(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        labels = payload.get("labels", [])
        if not isinstance(labels, list):
            return []
        return [
            {
                "name": str(label.get("name")).strip(),
                "color": str(label.get("color") or "").strip(),
            }
            for label in labels
            if isinstance(label, dict) and str(label.get("name", "")).strip()
        ]

    def build_search_text(self, title: str, body: str) -> str:
        return " ".join(part.strip() for part in [str(title or ""), str(body or "")] if part.strip())

    def _request(
        self,
        method: str,
        target: str,
        *,
        absolute: bool = False,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        response = self._raw_request(
            method,
            target,
            absolute=absolute,
            params=params,
            json_body=json_body,
            expected=expected,
        )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _raw_request(
        self,
        method: str,
        target: str,
        *,
        absolute: bool = False,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> requests.Response:
        if not self.is_configured():
            raise GitHubAPIError("GitHub token is not configured")

        url = target if absolute else urljoin(f"{self.config.api_base_url}/", target.lstrip("/"))

        try:
            response = self._session().request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            raise GitHubAPIError(f"GitHub request failed: {exc}") from exc

        if response.status_code not in expected:
            message = self._extract_message(response)
            raise GitHubAPIError(
                f"GitHub API {method} {url} returned {response.status_code}: {message}"
            )

        return response

    def _extract_message(self, response: requests.Response) -> str:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text[:300]
        return payload.get("message", response.text[:300])

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.config.token}" if self.config.token else "",
                    "User-Agent": "gh-dashboard/1.0",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
            )
            self._thread_local.session = session
        return session
