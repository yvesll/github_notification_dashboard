#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, render_template, request

from .ai_client import AIClient, AIClientError
from .config import load_config
from .github_api import GitHubAPI, GitHubAPIError
from .service import DashboardService, PollingWorker
from .storage import Storage


logging.basicConfig(level=logging.INFO)


def create_app() -> Flask:
    config = load_config()
    storage = Storage(config.storage.database_path)
    github_api = GitHubAPI(config.github)
    ai_client = AIClient(config.ai)
    service = DashboardService(config, storage, github_api, ai_client)
    service.bootstrap()

    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.debug = config.debug
    app.secret_key = config.secret_key
    app.config["DASHBOARD_CONFIG"] = config
    app.config["DASHBOARD_SERVICE"] = service

    if config.polling.enabled and _should_start_worker(app):
        worker = PollingWorker(service, config.polling.interval_seconds, config.polling.run_initial_sync)
        worker.start()
        app.config["DASHBOARD_WORKER"] = worker

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            defaults={
                "view": config.defaults.view,
                "status": config.defaults.status,
                "repository": config.defaults.repository,
                "label_include": config.defaults.label_include,
                "label_exclude": config.defaults.label_exclude,
                "reason": config.defaults.reason,
                "checks": config.defaults.checks,
                "review": config.defaults.review,
                "keyword": config.defaults.keyword,
            },
        )

    @app.get("/api/notifications")
    def notifications() -> tuple[str, int] | tuple[dict, int]:
        try:
            payload = service.list_notifications(
                {
                    "view": request.args.get("view", config.defaults.view),
                    "status": request.args.get("status", config.defaults.status),
                    "repository": request.args.get("repository", config.defaults.repository),
                    "label_include": request.args.get("label_include", config.defaults.label_include),
                    "label_exclude": request.args.get("label_exclude", config.defaults.label_exclude),
                    "reason": request.args.get("reason", config.defaults.reason),
                    "checks": request.args.get("checks", config.defaults.checks),
                    "review": request.args.get("review", config.defaults.review),
                    "keyword": request.args.get("keyword", config.defaults.keyword),
                }
            )
        except GitHubAPIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(payload), 200

    @app.post("/api/notifications/refresh")
    def refresh_notifications() -> tuple[dict, int]:
        try:
            payload = service.sync_notifications(source="manual")
        except GitHubAPIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(payload), 200

    @app.get("/api/notifications/<thread_id>")
    def notification_detail(thread_id: str) -> tuple[dict, int]:
        try:
            payload = service.get_notification_detail(thread_id)
        except KeyError:
            return jsonify({"error": "Notification not found"}), 404
        except GitHubAPIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(payload), 200

    @app.post("/api/notifications/<thread_id>/done")
    def set_done(thread_id: str) -> tuple[dict, int]:
        payload = request.get_json(silent=True) or {}
        try:
            item = service.toggle_done(thread_id, bool(payload.get("is_done", True)))
        except KeyError:
            return jsonify({"error": "Notification not found"}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except GitHubAPIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(item), 200

    @app.post("/api/notifications/<thread_id>/summary")
    def summary(thread_id: str) -> tuple[dict, int]:
        try:
            payload = service.summarize_notification(thread_id)
        except KeyError:
            return jsonify({"error": "Notification not found"}), 404
        except AIClientError as exc:
            return jsonify({"error": str(exc)}), 400
        except GitHubAPIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(payload), 200

    @app.post("/api/webhooks/github")
    def github_webhook() -> tuple[dict, int]:
        event = request.headers.get("X-GitHub-Event", "unknown")
        signature = request.headers.get("X-Hub-Signature-256")
        try:
            payload = service.webhook_event(event, request.data, signature)
        except PermissionError as exc:
            return jsonify({"error": str(exc)}), 403
        except GitHubAPIError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(payload), 200

    return app


def _should_start_worker(app: Flask) -> bool:
    if not app.debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"
