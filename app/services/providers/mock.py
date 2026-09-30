"""Deterministic offline provider (issue #2).

Used by the test suite and local development so the full pipeline (models,
routes, SSE streaming, UI) can run without network access or API keys. It
implements the same :class:`LLMProvider` contract as the real providers, which
is what the shared contract tests exercise.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from typing import Any

from app.services.providers.base import (
    LLMProvider,
    ProviderResponse,
    message_content,
    message_role,
    prepare_messages,
)


class MockProvider(LLMProvider):
    """Echoes a short canned response for the last user message."""

    name = "mock"
    models = ("mock-1",)

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay

    def _respond(self, messages: Iterable[Any]) -> str:
        last_user = ""
        for message in messages:
            if message_role(message) == "user":
                last_user = message_content(message)
        prefix = last_user[:80] + ("..." if len(last_user) > 80 else "")
        return (
            "This is a mock assistant response.\n\n"
            f"You said: {prefix}\n\n"
            "In development mode the mock provider echoes your input back. Configure "
            "OPENAI_API_KEY to enable real AI responses."
        )

    def chat(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict | None = None,
    ) -> ProviderResponse:
        started = time.perf_counter()
        if self.delay:
            time.sleep(self.delay)
        prepared = prepare_messages(messages, supports_vision=self.supports_vision)
        return ProviderResponse(
            content=self._respond(prepared),
            model=model or self.models[0],
            latency_seconds=time.perf_counter() - started,
        )

    def stream(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict | None = None,
    ) -> Iterator[str]:
        text = self._respond(prepare_messages(messages, supports_vision=self.supports_vision))
        for word in text.split(" "):
            if self.delay:
                time.sleep(self.delay)
            yield word + " "

    def stream_events(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict | None = None,
    ) -> Iterator[dict]:
        """Yield SSE-style events for the mock provider.

        Emits ``message_start``, one ``content`` delta per word, and a final
        ``message_end`` event carrying token usage. This mirrors the event
        protocol used by the real providers so the streaming endpoint and its
        integration tests can run offline.
        """
        prepared = prepare_messages(messages, supports_vision=self.supports_vision)
        text = self._respond(prepared)
        yield {"type": "message_start", "model": model or self.models[0]}
        for word in text.split(" "):
            if self.delay:
                time.sleep(self.delay)
            yield {"type": "content", "delta": word + " "}
        prompt_tokens = sum(len(message_content(m).split()) for m in prepared)
        completion_tokens = len(text.split())
        yield {
            "type": "message_end",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
