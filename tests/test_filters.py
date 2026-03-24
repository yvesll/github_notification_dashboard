#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gh_dashboard.config import (
    AIConfig,
    AppRuntimeConfig,
    DashboardConfig,
    DefaultFilterConfig,
    GitHubConfig,
    PollingConfig,
    StorageConfig,
    WebhookConfig,
)
from gh_dashboard.service import DashboardService
from gh_dashboard.storage import Storage


class FakeGitHubAPI:
    def __init__(self, state_by_thread: dict[str, dict[str, object]] | None = None) -> None:
        self.state_by_thread = state_by_thread or {}

    def is_configured(self) -> bool:
        return False

    def resolve_notification_state(self, notification: dict[str, object]) -> dict[str, object]:
        return self.state_by_thread[str(notification["id"])]

    def verify_webhook_signature(self, secret: str, payload: bytes, signature_header: str | None) -> bool:
        return True

    def mark_done(self, thread_id: str) -> None:
        return None


class FakeAIClient:
    def is_configured(self) -> bool:
        return False


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.storage = Storage(database_path)
        self.storage.initialize()
        self.storage.set_metadata("last_sync_at", "2026-03-21T00:00:00+00:00")
        self.config = self.build_config(database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def build_config(self, database_path: Path) -> DashboardConfig:
        return DashboardConfig(
            root_dir=database_path.parent,
            app=AppRuntimeConfig(host="127.0.0.1", port=5055, debug=False, secret_key="test"),
            storage=StorageConfig(database_path=database_path),
            github=GitHubConfig(
                token="",
                api_base_url="https://api.github.com",
                notifications_per_page=50,
                max_pages=4,
                request_timeout=20,
                mark_done_on_github=False,
            ),
            ai=AIConfig(
                provider="openai_compatible",
                api_key="",
                base_url="https://api.openai.com/v1",
                model="",
                anthropic_version="2023-06-01",
                max_output_tokens=600,
                summary_focus_prompt="Quickly summarize the description, the main discussion, and the latest status.",
                request_timeout=45,
                max_context_chars=24000,
            ),
            polling=PollingConfig(enabled=False, interval_seconds=300, run_initial_sync=False),
            defaults=DefaultFilterConfig(
                view="inbox",
                status="all",
                repository="all",
                label_include="all",
                label_exclude="all",
                reason="all",
                checks="all",
                review="all",
                keyword="",
            ),
            webhook=WebhookConfig(secret=""),
        )

    def make_service(self, state_by_thread: dict[str, dict[str, object]] | None = None) -> DashboardService:
        return DashboardService(
            self.config,
            self.storage,
            FakeGitHubAPI(state_by_thread),
            FakeAIClient(),
        )

    def seed_notifications(self) -> None:
        self.storage.upsert_notifications(
            [
                {
                    "thread_id": "1",
                    "repository_full_name": "acme/repo-a",
                    "repository_name": "repo-a",
                    "subject_type": "PullRequest",
                    "subject_title": "Watchdog timer cleanup",
                    "reason": "review_requested",
                    "unread": True,
                    "updated_at": "2026-03-21T01:00:00Z",
                    "last_read_at": None,
                    "status": "open",
                    "subject_url": "https://api.github.com/repos/acme/repo-a/pulls/1",
                    "web_url": "https://github.com/acme/repo-a/pull/1",
                    "checks_state": "passed",
                    "review_decision": "approved",
                    "labels": [
                        {"name": "drivers", "color": "1f883d"},
                        {"name": "priority", "color": "fbca04"},
                    ],
                    "search_text": "Watchdog timer cleanup improves watchdog startup and timeout flow.",
                    "in_inbox": True,
                    "raw_json": {},
                },
                {
                    "thread_id": "2",
                    "repository_full_name": "acme/repo-a",
                    "repository_name": "repo-a",
                    "subject_type": "PullRequest",
                    "subject_title": "Clock tree update",
                    "reason": "mention",
                    "unread": False,
                    "updated_at": "2026-03-20T01:00:00Z",
                    "last_read_at": "2026-03-20T02:00:00Z",
                    "status": "open",
                    "subject_url": "https://api.github.com/repos/acme/repo-a/pulls/2",
                    "web_url": "https://github.com/acme/repo-a/pull/2",
                    "checks_state": "other",
                    "review_decision": "changes_requested",
                    "labels": [{"name": "clock", "color": "5319e7"}],
                    "search_text": "Clock tree update adjusts pll settings.",
                    "in_inbox": False,
                    "raw_json": {},
                },
                {
                    "thread_id": "3",
                    "repository_full_name": "acme/repo-b",
                    "repository_name": "repo-b",
                    "subject_type": "Issue",
                    "subject_title": "Document board migration",
                    "reason": "comment",
                    "unread": False,
                    "updated_at": "2026-03-19T01:00:00Z",
                    "last_read_at": "2026-03-19T03:00:00Z",
                    "status": "closed",
                    "subject_url": "https://api.github.com/repos/acme/repo-b/issues/3",
                    "web_url": "https://github.com/acme/repo-b/issues/3",
                    "checks_state": "other",
                    "review_decision": "none",
                    "labels": [{"name": "docs", "color": "0e8a16"}],
                    "search_text": "Migration documentation for board updates.",
                    "in_inbox": False,
                    "raw_json": {},
                },
            ]
        )

    def test_keyword_label_status_intersection(self) -> None:
        self.seed_notifications()
        service = self.make_service()

        payload = service.list_notifications(
            {
                "view": "all",
                "repository": "acme/repo-a",
                "label_include": "drivers",
                "status": "open",
                "keyword": "watchdog",
            }
        )

        self.assertEqual([item["thread_id"] for item in payload["items"]], ["1"])

    def test_label_facets_only_for_selected_repository(self) -> None:
        self.seed_notifications()
        service = self.make_service()

        all_payload = service.list_notifications({"view": "all", "repository": "all"})
        repo_payload = service.list_notifications({"view": "all", "repository": "acme/repo-a"})

        self.assertEqual(all_payload["filters"]["labels"], [])
        self.assertEqual(
            [label["name"] for label in repo_payload["filters"]["labels"]],
            ["clock", "drivers", "priority"],
        )

    def test_label_include_and_exclude_can_be_combined(self) -> None:
        self.seed_notifications()
        service = self.make_service()

        payload = service.list_notifications(
            {
                "view": "all",
                "repository": "acme/repo-a",
                "label_include": "priority",
                "label_exclude": "clock",
            }
        )

        self.assertEqual([item["thread_id"] for item in payload["items"]], ["1"])

    def test_checks_filter_normalizes_other_states(self) -> None:
        self.seed_notifications()
        service = self.make_service(
            {
                "2": {
                    "status": "open",
                    "web_url": "https://github.com/acme/repo-a/pull/2",
                    "checks_state": "other",
                    "review_decision": "changes_requested",
                    "labels": [{"name": "clock", "color": "5319e7"}],
                    "search_text": "Clock tree update adjusts pll settings.",
                },
                "3": {
                    "status": "closed",
                    "web_url": "https://github.com/acme/repo-b/issues/3",
                    "checks_state": "other",
                    "review_decision": "none",
                    "labels": [{"name": "docs", "color": "0e8a16"}],
                    "search_text": "Migration documentation for board updates.",
                },
            }
        )

        payload = service.list_notifications({"view": "all", "checks": "other"})

        self.assertEqual({item["thread_id"] for item in payload["items"]}, {"2", "3"})
        self.assertTrue(all(item["checks_state"] == "other" for item in payload["items"]))

    def test_done_checks_filter_refreshes_archived_metadata(self) -> None:
        self.seed_notifications()
        service = self.make_service(
            {
                "2": {
                    "status": "open",
                    "web_url": "https://github.com/acme/repo-a/pull/2",
                    "checks_state": "passed",
                    "review_decision": "approved",
                    "labels": [{"name": "clock", "color": "5319e7"}],
                    "search_text": "Clock tree update adjusts pll settings.",
                },
                "3": {
                    "status": "closed",
                    "web_url": "https://github.com/acme/repo-b/issues/3",
                    "checks_state": "other",
                    "review_decision": "none",
                    "labels": [{"name": "docs", "color": "0e8a16"}],
                    "search_text": "Migration documentation for board updates.",
                },
            }
        )

        payload = service.list_notifications({"view": "done", "checks": "passed"})

        self.assertEqual([item["thread_id"] for item in payload["items"]], ["2"])


if __name__ == "__main__":
    unittest.main()
