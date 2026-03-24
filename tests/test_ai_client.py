from __future__ import annotations

import unittest
from unittest.mock import Mock

from gh_dashboard.ai_client import AIClient
from gh_dashboard.config import AIConfig


class AIClientTests(unittest.TestCase):
    def make_config(self, **overrides: object) -> AIConfig:
        config = {
            "provider": "openai_compatible",
            "api_key": "test-key",
            "base_url": "https://example.com/v1",
            "model": "test-model",
            "anthropic_version": "2023-06-01",
            "max_output_tokens": 600,
            "summary_focus_prompt": "Quickly summarize the description, the main discussion, and the latest status.",
            "request_timeout": 30,
            "max_context_chars": 24000,
        }
        config.update(overrides)
        return AIConfig(**config)

    def test_openai_compatible_request(self) -> None:
        client = AIClient(self.make_config())
        client.session.post = Mock(return_value=FakeResponse(200, {
            "choices": [{"message": {"content": '{"summary":"ok","action_items":["a"]}'}}],
        }))

        result = client.summarize({}, {
            "repository_full_name": "acme/repo",
            "subject_type": "PullRequest",
            "subject_title": "Title",
            "status": "open",
            "reason": "review_requested",
            "web_url": "https://github.com/acme/repo/pull/1",
        })

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(result["action_items"], ["a"])
        args, kwargs = client.session.post.call_args
        self.assertEqual(args[0], "https://example.com/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertIn("latest status", kwargs["json"]["messages"][0]["content"])

    def test_anthropic_request(self) -> None:
        client = AIClient(self.make_config(
            provider="anthropic",
            base_url="https://api.anthropic.com/v1",
            model="claude-sonnet-4-5",
        ))
        client.session.post = Mock(return_value=FakeResponse(200, {
            "content": [{"text": '{"summary":"anthropic","action_items":[]}'}],
        }))

        result = client.summarize({}, {
            "repository_full_name": "acme/repo",
            "subject_type": "PullRequest",
            "subject_title": "Title",
            "status": "open",
            "reason": "mention",
            "web_url": "https://github.com/acme/repo/pull/1",
        })

        self.assertEqual(result["summary"], "anthropic")
        args, kwargs = client.session.post.call_args
        self.assertEqual(args[0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(kwargs["headers"]["x-api-key"], "test-key")
        self.assertEqual(kwargs["headers"]["anthropic-version"], "2023-06-01")
        self.assertIn("latest status", kwargs["json"]["system"])

    def test_gemini_request(self) -> None:
        client = AIClient(self.make_config(
            provider="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.5-flash",
        ))
        client.session.post = Mock(return_value=FakeResponse(200, {
            "candidates": [{
                "content": {
                    "parts": [{"text": '{"summary":"gemini","action_items":["x"]}'}],
                }
            }],
        }))

        result = client.summarize({}, {
            "repository_full_name": "acme/repo",
            "subject_type": "Issue",
            "subject_title": "Title",
            "status": "open",
            "reason": "comment",
            "web_url": "https://github.com/acme/repo/issues/1",
        })

        self.assertEqual(result["summary"], "gemini")
        self.assertEqual(result["action_items"], ["x"])
        args, kwargs = client.session.post.call_args
        self.assertEqual(
            args[0],
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertIn("latest status", kwargs["json"]["system_instruction"]["parts"][0]["text"])

    def test_custom_summary_focus_prompt_is_used(self) -> None:
        client = AIClient(self.make_config(summary_focus_prompt="Focus on blockers and required actions first."))
        client.session.post = Mock(return_value=FakeResponse(200, {
            "choices": [{"message": {"content": '{"summary":"ok","action_items":["a"]}'}}],
        }))

        client.summarize({}, {
            "repository_full_name": "acme/repo",
            "subject_type": "PullRequest",
            "subject_title": "Title",
            "status": "open",
            "reason": "review_requested",
            "web_url": "https://github.com/acme/repo/pull/1",
        })

        _, kwargs = client.session.post.call_args
        self.assertIn("Focus on blockers and required actions first.", kwargs["json"]["messages"][0]["content"])


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, object]:
        return self._payload


if __name__ == "__main__":
    unittest.main()
