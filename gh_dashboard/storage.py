#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Iterator


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    thread_id TEXT PRIMARY KEY,
                    repository_full_name TEXT NOT NULL,
                    repository_name TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_title TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    unread INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    last_read_at TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    subject_url TEXT,
                    web_url TEXT,
                    checks_state TEXT NOT NULL DEFAULT 'none',
                    review_decision TEXT NOT NULL DEFAULT 'none',
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    search_text TEXT NOT NULL DEFAULT '',
                    in_inbox INTEGER NOT NULL DEFAULT 1,
                    raw_json TEXT NOT NULL,
                    last_synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thread_states (
                    thread_id TEXT PRIMARY KEY,
                    is_done INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES notifications(thread_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS detail_cache (
                    thread_id TEXT PRIMARY KEY,
                    source_updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES notifications(thread_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS summary_cache (
                    thread_id TEXT PRIMARY KEY,
                    source_updated_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    action_items_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES notifications(thread_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "notifications", "checks_state", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(conn, "notifications", "review_decision", "TEXT NOT NULL DEFAULT 'none'")
            self._ensure_column(conn, "notifications", "labels_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "notifications", "search_text", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "notifications", "in_inbox", "INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                """
                UPDATE notifications
                SET checks_state = CASE
                    WHEN checks_state = 'passed' THEN 'passed'
                    ELSE 'other'
                END
                """
            )

    def upsert_notifications(self, notifications: list[dict[str, object]]) -> None:
        if not notifications:
            return

        now = utc_now()
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO notifications (
                    thread_id,
                    repository_full_name,
                    repository_name,
                    subject_type,
                    subject_title,
                    reason,
                    unread,
                    updated_at,
                    last_read_at,
                    status,
                    subject_url,
                    web_url,
                    checks_state,
                    review_decision,
                    labels_json,
                    search_text,
                    in_inbox,
                    raw_json,
                    last_synced_at
                )
                VALUES (
                    :thread_id,
                    :repository_full_name,
                    :repository_name,
                    :subject_type,
                    :subject_title,
                    :reason,
                    :unread,
                    :updated_at,
                    :last_read_at,
                    :status,
                    :subject_url,
                    :web_url,
                    :checks_state,
                    :review_decision,
                    :labels_json,
                    :search_text,
                    :in_inbox,
                    :raw_json,
                    :last_synced_at
                )
                ON CONFLICT(thread_id) DO UPDATE SET
                    repository_full_name = excluded.repository_full_name,
                    repository_name = excluded.repository_name,
                    subject_type = excluded.subject_type,
                    subject_title = excluded.subject_title,
                    reason = excluded.reason,
                    unread = excluded.unread,
                    updated_at = excluded.updated_at,
                    last_read_at = excluded.last_read_at,
                    status = excluded.status,
                    subject_url = excluded.subject_url,
                    web_url = excluded.web_url,
                    checks_state = excluded.checks_state,
                    review_decision = excluded.review_decision,
                    labels_json = excluded.labels_json,
                    search_text = excluded.search_text,
                    in_inbox = excluded.in_inbox,
                    raw_json = excluded.raw_json,
                    last_synced_at = excluded.last_synced_at
                """,
                [
                    {
                        **item,
                        "unread": 1 if item.get("unread") else 0,
                        "labels_json": json.dumps(self._normalize_labels(item.get("labels", [])), ensure_ascii=False),
                        "in_inbox": 1 if item.get("in_inbox", True) else 0,
                        "raw_json": json.dumps(item["raw_json"], ensure_ascii=False),
                        "last_synced_at": now,
                    }
                    for item in notifications
                ],
            )

    def list_notifications(
        self,
        *,
        view: str = "inbox",
        status: str = "all",
        repository: str = "all",
        reason: str = "all",
        checks: str = "all",
        review: str = "all",
        keyword: str = "",
    ) -> list[dict[str, object]]:
        where = ["1 = 1"]
        params: dict[str, object] = {}

        if view == "inbox":
            where.append("n.in_inbox = 1")
        elif view == "done":
            where.append("n.in_inbox = 0")

        if status != "all":
            where.append("n.status = :status")
            params["status"] = status

        if repository != "all":
            where.append("n.repository_full_name = :repository")
            params["repository"] = repository

        if reason != "all":
            where.append("n.reason = :reason")
            params["reason"] = reason

        if checks != "all":
            if checks == "passed":
                where.append("n.checks_state = 'passed'")
            else:
                where.append("n.checks_state != 'passed'")

        if review != "all":
            where.append("n.review_decision = :review")
            params["review"] = review

        keyword = keyword.strip()
        if keyword:
            where.append("(lower(n.subject_title) LIKE :keyword OR lower(n.search_text) LIKE :keyword)")
            params["keyword"] = f"%{keyword.lower()}%"

        query = f"""
            SELECT
                n.*
            FROM notifications n
            WHERE {' AND '.join(where)}
            ORDER BY datetime(n.updated_at) DESC
        """

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._serialize_notification_row(row) for row in rows]

    def get_notification(self, thread_id: str) -> dict[str, object] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    n.*
                FROM notifications n
                WHERE n.thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        return self._serialize_notification_row(row) if row else None

    def get_notifications_by_thread_ids(self, thread_ids: list[str]) -> dict[str, dict[str, object]]:
        if not thread_ids:
            return {}

        placeholders = ", ".join("?" for _ in thread_ids)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    n.*
                FROM notifications n
                WHERE n.thread_id IN ({placeholders})
                """,
                thread_ids,
            ).fetchall()
        return {
            row["thread_id"]: self._serialize_notification_row(row)
            for row in rows
        }

    def set_in_inbox(self, thread_id: str, in_inbox: bool) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE notifications
                SET in_inbox = ?, last_synced_at = ?
                WHERE thread_id = ?
                """,
                (1 if in_inbox else 0, utc_now(), thread_id),
            )

    def reconcile_inbox_threads(self, active_thread_ids: list[str]) -> None:
        with self.connection() as conn:
            if active_thread_ids:
                placeholders = ", ".join("?" for _ in active_thread_ids)
                conn.execute(
                    f"""
                    UPDATE notifications
                    SET in_inbox = 0, unread = 0
                    WHERE thread_id NOT IN ({placeholders})
                    """,
                    active_thread_ids,
                )
            else:
                conn.execute("UPDATE notifications SET in_inbox = 0, unread = 0")

    def get_facets(self) -> dict[str, list[str]]:
        with self.connection() as conn:
            repositories = [
                row["repository_full_name"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT repository_full_name
                    FROM notifications
                    ORDER BY lower(repository_full_name)
                    """
                ).fetchall()
            ]
            reasons = [
                row["reason"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT reason
                    FROM notifications
                    ORDER BY lower(reason)
                    """
                ).fetchall()
            ]
            statuses = [
                row["status"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT status
                    FROM notifications
                    ORDER BY CASE status
                        WHEN 'open' THEN 1
                        WHEN 'closed' THEN 2
                        WHEN 'merged' THEN 3
                        ELSE 4
                    END
                    """
                ).fetchall()
            ]
            checks_states = [
                row["checks_state"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT checks_state
                    FROM notifications
                    ORDER BY CASE checks_state
                        WHEN 'passed' THEN 1
                        WHEN 'other' THEN 2
                        ELSE 3
                    END
                    """
                ).fetchall()
            ]
            review_states = [
                row["review_decision"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT review_decision
                    FROM notifications
                    ORDER BY CASE review_decision
                        WHEN 'changes_requested' THEN 1
                        WHEN 'approved' THEN 2
                        WHEN 'commented' THEN 3
                        WHEN 'none' THEN 4
                        ELSE 5
                    END
                    """
                ).fetchall()
            ]

        return {
            "repositories": repositories,
            "reasons": reasons,
            "statuses": statuses,
            "checks_states": checks_states,
            "review_states": review_states,
        }

    def get_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            total = conn.execute("SELECT COUNT(*) AS count FROM notifications").fetchone()["count"]
            done = conn.execute("SELECT COUNT(*) AS count FROM notifications WHERE in_inbox = 0").fetchone()["count"]
            inbox = conn.execute("SELECT COUNT(*) AS count FROM notifications WHERE in_inbox = 1").fetchone()["count"]
        return {
            "all": total,
            "done": done,
            "inbox": inbox,
        }

    def list_archived_notifications(self, repository: str = "all") -> list[dict[str, object]]:
        params: list[object] = []
        query = """
            SELECT
                n.*
            FROM notifications n
            WHERE n.in_inbox = 0
        """
        if repository != "all":
            query += " AND n.repository_full_name = ?"
            params.append(repository)

        query += " ORDER BY datetime(n.updated_at) DESC"

        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._serialize_notification_row(row) for row in rows]

    def get_detail_cache(self, thread_id: str, source_updated_at: str) -> dict[str, object] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT payload_json
                FROM detail_cache
                WHERE thread_id = ? AND source_updated_at = ?
                """,
                (thread_id, source_updated_at),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def set_detail_cache(self, thread_id: str, source_updated_at: str, payload: dict[str, object]) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO detail_cache (thread_id, source_updated_at, payload_json, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    source_updated_at = excluded.source_updated_at,
                    payload_json = excluded.payload_json,
                    fetched_at = excluded.fetched_at
                """,
                (
                    thread_id,
                    source_updated_at,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def get_summary_cache(self, thread_id: str, source_updated_at: str) -> dict[str, object] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT summary, action_items_json, response_json, generated_at
                FROM summary_cache
                WHERE thread_id = ? AND source_updated_at = ?
                """,
                (thread_id, source_updated_at),
            ).fetchone()
        if not row:
            return None

        return {
            "summary": row["summary"],
            "action_items": json.loads(row["action_items_json"]),
            "generated_at": row["generated_at"],
            "raw": json.loads(row["response_json"]),
        }

    def set_summary_cache(
        self,
        thread_id: str,
        source_updated_at: str,
        summary: str,
        action_items: list[str],
        raw_response: dict[str, object],
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO summary_cache (
                    thread_id,
                    source_updated_at,
                    summary,
                    action_items_json,
                    response_json,
                    generated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    source_updated_at = excluded.source_updated_at,
                    summary = excluded.summary,
                    action_items_json = excluded.action_items_json,
                    response_json = excluded.response_json,
                    generated_at = excluded.generated_at
                """,
                (
                    thread_id,
                    source_updated_at,
                    summary,
                    json.dumps(action_items, ensure_ascii=False),
                    json.dumps(raw_response, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def get_metadata(self, key: str) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def _serialize_notification_row(self, row: sqlite3.Row) -> dict[str, object]:
        raw_json = json.loads(row["raw_json"])
        checks_state = str(row["checks_state"] or "other")
        checks_state = "passed" if checks_state == "passed" else "other"
        return {
            "thread_id": row["thread_id"],
            "repository_full_name": row["repository_full_name"],
            "repository_name": row["repository_name"],
            "subject_type": row["subject_type"],
            "subject_title": row["subject_title"],
            "reason": row["reason"],
            "unread": bool(row["unread"]),
            "updated_at": row["updated_at"],
            "last_read_at": row["last_read_at"],
            "status": row["status"],
            "subject_url": row["subject_url"],
            "web_url": row["web_url"],
            "checks_state": checks_state,
            "review_decision": row["review_decision"],
            "labels": self._normalize_labels(json.loads(row["labels_json"])),
            "search_text": row["search_text"],
            "in_inbox": bool(row["in_inbox"]),
            "is_done": not bool(row["in_inbox"]),
            "raw_json": raw_json,
        }

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _normalize_labels(self, labels: object) -> list[dict[str, str]]:
        if not isinstance(labels, list):
            return []

        normalized: list[dict[str, str]] = []
        for label in labels:
            if isinstance(label, str):
                name = label.strip()
                if name:
                    normalized.append({"name": name, "color": ""})
                continue

            if isinstance(label, dict):
                name = str(label.get("name") or "").strip()
                color = str(label.get("color") or "").strip()
                if name:
                    normalized.append({"name": name, "color": color})

        return normalized
