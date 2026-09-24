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
        return ProviderResponse(
            content=self._respond(messages),
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
        text = self._respond(messages)
        for word in text.split(" "):
            if self.delay:
                time.sleep(self.delay)
            yield word + " "
