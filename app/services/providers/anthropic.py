"""Anthropic Messages API provider (issue #2).

Demonstrates that the abstraction is genuinely vendor-neutral: Anthropic uses a
different endpoint, auth header, request shape (a top-level ``system`` field),
and response envelope (content blocks + ``input_tokens``/``output_tokens``). All
of that is encapsulated here behind the same :class:`LLMProvider` contract.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable, Iterator
from typing import Any

import requests

from app.services.providers.base import (
    LLMProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    normalize_messages,
)

TIMEOUT_SECONDS = 60
DEFAULT_MODELS = ("claude-3-5-sonnet-latest", "claude-3-5-haiku-latest")


class AnthropicProvider(LLMProvider):
    """Client for the Anthropic ``/v1/messages`` API."""

    name = "anthropic"
    models = DEFAULT_MODELS

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        version: str | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = (
            base_url or os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")
        ).rstrip("/")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        self.version = version or os.getenv("ANTHROPIC_VERSION", "2023-06-01")
        max_tokens_env = os.getenv("ANTHROPIC_MAX_TOKENS", "1024")
        self.max_tokens = int(max_tokens if max_tokens is not None else max_tokens_env)

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderConfigurationError(
                "ANTHROPIC_API_KEY is not set. Configure an LLM provider or use the "
                "mock provider for development.",
                provider=self.name,
            )

    def _headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
            "Content-Type": "application/json",
        }

    def _split_system(self, messages: Iterable[Any]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        chat: list[dict] = []
        for message in normalize_messages(messages):
            if message["role"] == "system":
                system_parts.append(message["content"])
            else:
                chat.append(message)
        return "\n\n".join(system_parts), chat

    def _payload(
        self,
        messages: Iterable[Any],
        *,
        model: str | None,
        params: dict | None,
        stream: bool,
    ) -> dict:
        system, chat = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": model or self.model,
            "max_tokens": self.max_tokens,
            "messages": chat,
            "stream": stream,
        }
        if system:
            payload["system"] = system
        if params:
            payload.update(params)
        return payload

    def _post(self, payload: dict, *, stream: bool) -> requests.Response:
        try:
            return requests.post(
                f"{self.base_url}/messages",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT_SECONDS,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise ProviderUnavailableError(
                f"Anthropic request failed: {exc}", provider=self.name
            ) from exc

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.status_code < 400:
            return
        message = f"Anthropic request failed with status {response.status_code}."
        if response.status_code in (401, 403):
            raise ProviderAuthenticationError(message, provider=self.name)
        if response.status_code == 429:
            raise ProviderRateLimitError(message, provider=self.name)
        if response.status_code >= 500:
            raise ProviderUnavailableError(message, provider=self.name)
        raise ProviderResponseError(message, provider=self.name)

    def chat(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict | None = None,
    ) -> ProviderResponse:
        self._require_key()
        payload = self._payload(messages, model=model, params=params, stream=False)
        started = time.perf_counter()
        response = self._post(payload, stream=False)
        self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                "Anthropic returned a non-JSON response.", provider=self.name
            ) from exc
        try:
            content = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if isinstance(block, dict) and block.get("type") == "text"
            )
        except (AttributeError, TypeError) as exc:
            raise ProviderResponseError(
                "Anthropic returned an unexpected response shape.", provider=self.name
            ) from exc
        usage = data.get("usage") or {}
        return ProviderResponse(
            content=content,
            model=data.get("model") or payload["model"],
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            latency_seconds=time.perf_counter() - started,
        )

    def stream(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict | None = None,
    ) -> Iterator[str]:
        self._require_key()
        payload = self._payload(messages, model=model, params=params, stream=True)
        response = self._post(payload, stream=True)
        self._raise_for_status(response)
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[len("data: ") :].strip())
            except ValueError:
                continue
            if event.get("type") == "content_block_delta":
                text = (event.get("delta") or {}).get("text", "")
                if text:
                    yield text
