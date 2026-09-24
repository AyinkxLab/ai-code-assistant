"""OpenAI-compatible chat-completions provider (issue #2).

Targets ``/chat/completions`` so it can talk to OpenAI, an Azure/OpenAI-compatible
gateway, or a local server (e.g. llama.cpp) by changing ``OPENAI_BASE_URL``. All
HTTP and response-shape handling stays inside this module; callers only ever see
:class:`ProviderResponse` or a :class:`ProviderError`.
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
DEFAULT_MODELS = ("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1")


class OpenAIProvider(LLMProvider):
    """Client for OpenAI-compatible ``/chat/completions`` endpoints."""

    name = "openai"
    models = DEFAULT_MODELS

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        env_temperature = os.getenv("OPENAI_TEMPERATURE", "0.7")
        self.temperature = float(temperature if temperature is not None else env_temperature)

    def _require_key(self) -> None:
        if not self.api_key:
            raise ProviderConfigurationError(
                "OPENAI_API_KEY is not set. Configure an LLM provider or use the "
                "mock provider for development.",
                provider=self.name,
            )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: Iterable[Any],
        *,
        model: str | None,
        params: dict | None,
        stream: bool,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": normalize_messages(messages),
            "temperature": self.temperature,
            "stream": stream,
        }
        if params:
            payload.update(params)
        return payload

    def _post(self, payload: dict, *, stream: bool) -> requests.Response:
        try:
            return requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT_SECONDS,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise ProviderUnavailableError(
                f"OpenAI request failed: {exc}", provider=self.name
            ) from exc

    def _raise_for_status(self, response: requests.Response) -> None:
        if response.status_code < 400:
            return
        message = f"OpenAI request failed with status {response.status_code}."
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
                "OpenAI returned a non-JSON response.", provider=self.name
            ) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                "OpenAI returned an unexpected response shape.", provider=self.name
            ) from exc
        usage = data.get("usage") or {}
        return ProviderResponse(
            content=content or "",
            model=data.get("model") or payload["model"],
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
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
            chunk_payload = line[len("data: ") :].strip()
            if chunk_payload == "[DONE]":
                break
            try:
                chunk = json.loads(chunk_payload)
                delta = chunk["choices"][0]["delta"].get("content", "")
            except (KeyError, IndexError, TypeError, ValueError):
                delta = ""
            if delta:
                yield delta
