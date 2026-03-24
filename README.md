# GitHub Review Desk

A Flask dashboard for triaging GitHub PR and issue notifications with inline AI summaries, cached discussion detail, configurable polling, and webhook-triggered refreshes.

## Features

- Pulls PR and issue notifications from the GitHub Notifications API.
- Supports manual refresh, background polling, and GitHub webhook-triggered sync.
- Shows repository, type, status, reason, timestamps, unread state, and an Inbox / Done / All workflow aligned to the latest GitHub sync.
- Supports list filters for PR checks and review state, including `checks passed` and `changes requested`.
- Expands each row to render descriptions, comments, review state, and review comments as Markdown.
- Generates AI summaries on demand and supports multiple AI providers, with a local heuristic fallback when the configured provider is unavailable.
- Reads runtime settings from `config.toml`, including GitHub token, AI endpoint, and polling defaults.
- Follows system `prefers-color-scheme` for light and dark mode.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 app.py
```

Open [http://127.0.0.1:5055](http://127.0.0.1:5055).
If that port is already occupied, the app will automatically fall back to a nearby port and print the exact URL in the terminal.

## Config

A sanitized template is available at [`config.template.toml`](./config.template.toml).

Use [`config.toml`](./config.toml) for your local runtime values:

- `github.token`: GitHub personal access token with notifications and repo read access.
- `ai.provider`: `openai_compatible`, `anthropic`, or `gemini`
- `ai.api_key`, `ai.base_url`, `ai.model`: provider credentials and model selection
- `ai.anthropic_version`: required Anthropic API version header when using `anthropic`
- `ai.max_output_tokens`: shared output token cap for Anthropic and Gemini
- `ai.summary_focus_prompt`: summary-specific instruction that steers what the AI should emphasize
- `polling.interval_seconds`: Background sync cadence.
- `defaults.*`: Default list filters on first page load, including `label_include`, `label_exclude`, and `keyword`
- `webhook.secret`: Optional secret for validating `POST /api/webhooks/github`.

Environment placeholders like `"${GITHUB_TOKEN}"` are supported.

For multi-select label defaults, use TOML arrays:

```toml
[defaults]
label_include = ["platform: NXP", "area: Timer"]
label_exclude = ["docs"]
```

## AI Providers

The dashboard supports these providers directly:

- OpenAI-compatible chat completion APIs
- Anthropic Messages API
- Google Gemini `generateContent` API

### OpenAI-compatible

```toml
[ai]
provider = "openai_compatible"
api_key = "${OPENAI_API_KEY}"
base_url = "https://api.openai.com/v1"
model = "gpt-4.1-mini"
max_output_tokens = 600
summary_focus_prompt = "Quickly summarize the description, the main discussion, and the latest status."
request_timeout = 45
max_context_chars = 240000
```

### Anthropic

```toml
[ai]
provider = "anthropic"
api_key = "${ANTHROPIC_API_KEY}"
base_url = "https://api.anthropic.com/v1"
model = "claude-sonnet-4-5"
anthropic_version = "2023-06-01"
max_output_tokens = 600
summary_focus_prompt = "Quickly summarize the description, the main discussion, and the latest status."
request_timeout = 45
max_context_chars = 240000
```

### Gemini

```toml
[ai]
provider = "gemini"
api_key = "${GEMINI_API_KEY}"
base_url = "https://generativelanguage.googleapis.com/v1beta"
model = "gemini-2.5-flash"
max_output_tokens = 600
summary_focus_prompt = "Quickly summarize the description, the main discussion, and the latest status."
request_timeout = 45
max_context_chars = 240000
```

### Custom Summary Focus

You can steer the AI summary without changing application code:

```toml
[ai]
summary_focus_prompt = "Prioritize risk, blockers, required actions, and the latest reviewer position."
```

Default behavior:

- Quickly summarize the description
- Summarize the main discussion
- Highlight the latest status

Official references:

- [OpenAI Chat Completions](https://platform.openai.com/docs/api-reference/chat/create)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages-examples)
- [Gemini text generation](https://ai.google.dev/gemini-api/docs/text-generation)

## Webhook

Point a GitHub webhook at:

```text
POST /api/webhooks/github
```

Useful events:

- `issues`
- `issue_comment`
- `pull_request`
- `pull_request_review`
- `pull_request_review_comment`

## Storage

- SQLite cache: `data/gh_dashboard.sqlite3`
- Detail cache and AI summary cache are invalidated automatically when GitHub reports a newer `updated_at` timestamp for a thread.
- GitHub's REST API does not expose a full historical Done list, so the Done view becomes accurate for threads this app has already seen during sync.
- Inbox is synced from the current unread notifications feed; Local Done is a local archive of items no longer present in that unread feed.
