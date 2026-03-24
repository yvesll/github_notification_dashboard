#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
from typing import Any

import requests

from .config import AIConfig


OPENAI_SYSTEM_PROMPT = (
    "You summarize GitHub review threads for an engineering dashboard. "
    "Return strict JSON with keys summary and action_items. "
    "summary should be a short paragraph. action_items should be an array of concise strings."
)


class AIClientError(RuntimeError):
    pass


class AIClient:
    def __init__(self, config: AIConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def is_configured(self) -> bool:
        return self.config.enabled

    def summarize(self, detail: dict[str, Any], notification: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured():
            raise AIClientError("AI is not configured")

        prompt = self._build_prompt(detail, notification)
        provider = self.config.provider

        if provider in {"openai", "openai_compatible"}:
            payload, content = self._request_openai_compatible(prompt)
        elif provider == "anthropic":
            payload, content = self._request_anthropic(prompt)
        elif provider == "gemini":
            payload, content = self._request_gemini(prompt)
        else:
            raise AIClientError(f"Unsupported AI provider: {provider}")

        parsed = self._parse_response(content)
        return {
            "summary": parsed["summary"],
            "action_items": parsed["action_items"],
            "raw": {
                "provider": provider,
                "response": payload,
            },
        }

    def _request_openai_compatible(self, prompt: str) -> tuple[dict[str, Any], str]:
        response = self._post_json(
            f"{self._resolved_base_url()}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            payload={
                "model": self.config.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": prompt},
                ],
            },
            provider="openai_compatible",
        )
        content = response["choices"][0]["message"]["content"]
        return response, content

    def _request_anthropic(self, prompt: str) -> tuple[dict[str, Any], str]:
        response = self._post_json(
            f"{self._resolved_base_url()}/messages",
            headers={
                "x-api-key": self.config.api_key,
                "anthropic-version": self.config.anthropic_version,
                "Content-Type": "application/json",
            },
            payload={
                "model": self.config.model,
                "max_tokens": self.config.max_output_tokens,
                "temperature": 0.2,
                "system": self._system_prompt(),
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
            },
            provider="anthropic",
        )
        content = "".join(
            part.get("text", "")
            for part in response.get("content", [])
            if isinstance(part, dict)
        )
        return response, content

    def _request_gemini(self, prompt: str) -> tuple[dict[str, Any], str]:
        response = self._post_json(
            f"{self._resolved_base_url()}/models/{self.config.model}:generateContent",
            headers={
                "x-goog-api-key": self.config.api_key,
                "Content-Type": "application/json",
            },
            payload={
                "system_instruction": {
                    "parts": [{"text": self._system_prompt()}],
                },
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    }
                ],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "maxOutputTokens": self.config.max_output_tokens,
                    "temperature": 0.2,
                },
            },
            provider="gemini",
        )
        candidates = response.get("candidates", [])
        if not candidates:
            raise AIClientError("Gemini response did not contain any candidates")
        content = "".join(
            part.get("text", "")
            for part in candidates[0].get("content", {}).get("parts", [])
            if isinstance(part, dict)
        )
        return response, content

    def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        provider: str,
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                url,
                headers=headers,
                json=payload,
                timeout=self.config.request_timeout,
            )
        except requests.RequestException as exc:
            raise AIClientError(f"AI request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AIClientError(self._format_api_error(response, provider))

        return response.json()

    def _resolved_base_url(self) -> str:
        if self.config.base_url:
            return self.config.base_url

        defaults = {
            "openai": "https://api.openai.com/v1",
            "openai_compatible": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
        }
        return defaults.get(self.config.provider, "")

    def _build_prompt(self, detail: dict[str, Any], notification: dict[str, Any]) -> str:
        sections = [
            f"Repository: {notification['repository_full_name']}",
            f"Type: {notification['subject_type']}",
            f"Title: {notification['subject_title']}",
            f"Status: {notification['status']}",
            f"Reason: {notification['reason']}",
            f"Author: {detail.get('author') or 'unknown'}",
            f"URL: {detail.get('html_url') or notification.get('web_url') or 'n/a'}",
            "",
            "Description:",
            detail.get("body_text") or detail.get("body") or "(empty)",
            "",
        ]

        if detail.get("reviews"):
            sections.extend(
                [
                    "Reviews:",
                    *[
                        (
                            f"- {review.get('author', 'unknown')} "
                            f"[{review.get('state', 'COMMENTED')}] "
                            f"{review.get('submitted_at', '')}: "
                            f"{(review.get('body_text') or review.get('body') or '').strip() or '(no body)'}"
                        )
                        for review in detail["reviews"]
                    ],
                    "",
                ]
            )

        comments: list[str] = []
        for comment in detail.get("comments", []):
            comments.append(
                f"- {comment.get('author', 'unknown')} {comment.get('created_at', '')}: "
                f"{(comment.get('body_text') or comment.get('body') or '').strip() or '(no body)'}"
            )
        for comment in detail.get("review_comments", []):
            comments.append(
                f"- {comment.get('author', 'unknown')} {comment.get('created_at', '')}: "
                f"{(comment.get('body_text') or comment.get('body') or '').strip() or '(no body)'}"
            )

        if comments:
            sections.extend(["Discussion:", *comments, ""])

        prompt = "\n".join(sections)
        return prompt[: self.config.max_context_chars]

    def _system_prompt(self) -> str:
        focus = self.config.summary_focus_prompt.strip()
        if not focus:
            return OPENAI_SYSTEM_PROMPT
        return f"{OPENAI_SYSTEM_PROMPT} Focus: {focus}"

    def _parse_response(self, content: str) -> dict[str, Any]:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            payload = self._extract_json_block(content)

        summary = str(payload.get("summary", "")).strip()
        action_items_raw = payload.get("action_items", [])
        if not isinstance(action_items_raw, list):
            action_items_raw = [str(action_items_raw)]

        action_items = [str(item).strip() for item in action_items_raw if str(item).strip()]
        if not summary:
            raise AIClientError("AI response did not contain a summary")

        return {
            "summary": summary,
            "action_items": action_items,
        }

    def _extract_json_block(self, content: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return {"summary": content.strip(), "action_items": []}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AIClientError("AI response was not valid JSON") from exc

    def _format_api_error(self, response: requests.Response, provider: str) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"AI API returned {response.status_code}: {response.text[:200]}"

        if provider == "anthropic":
            error = payload.get("error", {})
            message = str(error.get("message") or "").strip()
            return message or f"Anthropic API returned {response.status_code}"

        if provider == "gemini":
            error = payload.get("error", {})
            message = str(error.get("message") or "").strip()
            return message or f"Gemini API returned {response.status_code}"

        error = payload.get("error", {})
        if not isinstance(error, dict):
            return f"AI API returned {response.status_code}"

        error_type = str(error.get("type") or error.get("code") or "").lower()
        message = str(error.get("message") or "").strip()

        if error_type == "no_available_providers":
            return "Configured AI gateway currently has no available upstream providers"

        if error_type == "rate_limit_exceeded":
            limit_type = str(error.get("limit_type") or "").lower()
            if limit_type == "concurrent_sessions":
                return "Configured AI gateway hit its concurrent session limit"
            return "Configured AI gateway is rate limited"

        if message:
            return f"AI API returned {response.status_code}: {message}"

        return f"AI API returned {response.status_code}"
