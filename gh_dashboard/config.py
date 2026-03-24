#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


def _expand_env(value: object) -> object:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    return value


def _section(data: dict[str, object], key: str) -> dict[str, object]:
    section = data.get(key, {})
    return section if isinstance(section, dict) else {}


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.lower() == "all":
            return []
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return []


@dataclass(slots=True)
class AppRuntimeConfig:
    host: str
    port: int
    debug: bool
    secret_key: str


@dataclass(slots=True)
class StorageConfig:
    database_path: Path


@dataclass(slots=True)
class GitHubConfig:
    token: str
    api_base_url: str
    notifications_per_page: int
    max_pages: int
    request_timeout: int
    mark_done_on_github: bool


@dataclass(slots=True)
class AIConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    anthropic_version: str
    max_output_tokens: int
    summary_focus_prompt: str
    request_timeout: int
    max_context_chars: int

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)


@dataclass(slots=True)
class PollingConfig:
    enabled: bool
    interval_seconds: int
    run_initial_sync: bool


@dataclass(slots=True)
class DefaultFilterConfig:
    view: str
    status: str
    repository: str
    label_include: list[str]
    label_exclude: list[str]
    reason: str
    checks: str
    review: str
    keyword: str


@dataclass(slots=True)
class WebhookConfig:
    secret: str


@dataclass(slots=True)
class DashboardConfig:
    root_dir: Path
    app: AppRuntimeConfig
    storage: StorageConfig
    github: GitHubConfig
    ai: AIConfig
    polling: PollingConfig
    defaults: DefaultFilterConfig
    webhook: WebhookConfig

    @property
    def host(self) -> str:
        return self.app.host

    @property
    def port(self) -> int:
        return self.app.port

    @property
    def debug(self) -> bool:
        return self.app.debug

    @property
    def secret_key(self) -> str:
        return self.app.secret_key


def load_config(config_path: Path | None = None) -> DashboardConfig:
    root_dir = Path(config_path or os.getenv("GH_DASHBOARD_CONFIG") or "config.toml").expanduser().resolve()

    if not root_dir.exists():
        raise FileNotFoundError(f"Config file not found: {root_dir}")

    with root_dir.open("rb") as handle:
        raw = tomllib.load(handle)

    app = _section(raw, "app")
    storage = _section(raw, "storage")
    github = _section(raw, "github")
    ai = _section(raw, "ai")
    polling = _section(raw, "polling")
    defaults = _section(raw, "defaults")
    webhook = _section(raw, "webhook")

    database_path = Path(str(_expand_env(storage.get("database_path", "data/gh_dashboard.sqlite3"))))
    if not database_path.is_absolute():
        database_path = root_dir.parent / database_path

    return DashboardConfig(
        root_dir=root_dir.parent,
        app=AppRuntimeConfig(
            host=str(_expand_env(app.get("host", "127.0.0.1"))),
            port=int(_expand_env(app.get("port", 5000))),
            debug=bool(_expand_env(app.get("debug", False))),
            secret_key=str(_expand_env(app.get("secret_key", "replace-me"))),
        ),
        storage=StorageConfig(database_path=database_path),
        github=GitHubConfig(
            token=str(_expand_env(github.get("token", ""))),
            api_base_url=str(_expand_env(github.get("api_base_url", "https://api.github.com"))).rstrip("/"),
            notifications_per_page=int(_expand_env(github.get("notifications_per_page", 50))),
            max_pages=int(_expand_env(github.get("max_pages", 4))),
            request_timeout=int(_expand_env(github.get("request_timeout", 20))),
            mark_done_on_github=bool(_expand_env(github.get("mark_done_on_github", False))),
        ),
        ai=AIConfig(
            provider=str(_expand_env(ai.get("provider", "openai_compatible"))).strip().lower(),
            api_key=str(_expand_env(ai.get("api_key", ""))),
            base_url=str(_expand_env(ai.get("base_url", ""))).rstrip("/"),
            model=str(_expand_env(ai.get("model", "gpt-4.1-mini"))),
            anthropic_version=str(_expand_env(ai.get("anthropic_version", "2023-06-01"))),
            max_output_tokens=int(_expand_env(ai.get("max_output_tokens", 600))),
            summary_focus_prompt=str(
                _expand_env(
                    ai.get(
                        "summary_focus_prompt",
                        "Quickly summarize the description, the main discussion, and the latest status.",
                    )
                )
            ),
            request_timeout=int(_expand_env(ai.get("request_timeout", 45))),
            max_context_chars=int(_expand_env(ai.get("max_context_chars", 24000))),
        ),
        polling=PollingConfig(
            enabled=bool(_expand_env(polling.get("enabled", True))),
            interval_seconds=int(_expand_env(polling.get("interval_seconds", 300))),
            run_initial_sync=bool(_expand_env(polling.get("run_initial_sync", True))),
        ),
        defaults=DefaultFilterConfig(
            view=str(_expand_env(defaults.get("view", "inbox"))),
            status=str(_expand_env(defaults.get("status", "all"))),
            repository=str(_expand_env(defaults.get("repository", "all"))),
            label_include=_string_list(_expand_env(defaults.get("label_include", []))),
            label_exclude=_string_list(_expand_env(defaults.get("label_exclude", []))),
            reason=str(_expand_env(defaults.get("reason", "all"))),
            checks=str(_expand_env(defaults.get("checks", "all"))),
            review=str(_expand_env(defaults.get("review", "all"))),
            keyword=str(_expand_env(defaults.get("keyword", ""))),
        ),
        webhook=WebhookConfig(
            secret=str(_expand_env(webhook.get("secret", ""))),
        ),
    )
