"""Tests for the provider-agnostic LLM layer (#2).

Exercises the registry, the uniform ``ProviderResponse``/error hierarchy, the
shared contract (a fake provider and the mock provider pass the same checks),
and the OpenAI/Anthropic request/response handling with ``requests`` mocked.
"""

import pytest

from app.services import providers
from app.services.providers import (
    AnthropicProvider,
    MockProvider,
    OpenAIProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    UnknownProviderError,
    available_providers,
    get_provider,
    register_provider,
    unregister_provider,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", lines=None):
        self.status_code = status_code
        self._data = data
        self.text = text
        self._lines = lines or []

    def json(self):
        return self._data

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class FakeProvider(providers.LLMProvider):
    name = "fake"
    models = ("fake-1",)

    def __init__(self):
        self.calls = []

    def chat(self, messages, *, model=None, params=None):
        self.calls.append(("chat", list(messages)))
        return ProviderResponse(
            content="hello",
            model=model or self.models[0],
            prompt_tokens=3,
            completion_tokens=2,
            latency_seconds=0.01,
        )

    def stream(self, messages, *, model=None, params=None):
        self.calls.append(("stream", list(messages)))
        yield "he"
        yield "llo"


class TestRegistry:
    def test_builtin_providers_registered(self):
        assert {"mock", "openai", "anthropic"} <= set(available_providers())

    def test_register_and_get(self):
        register_provider("fake", FakeProvider)
        try:
            assert isinstance(get_provider("fake"), FakeProvider)
        finally:
            unregister_provider("fake")

    def test_unknown_provider_raises_clear_error(self):
        with pytest.raises(UnknownProviderError) as excinfo:
            get_provider("does-not-exist")
        assert "does-not-exist" in str(excinfo.value)
        assert "Available providers" in str(excinfo.value)

    def test_env_selects_provider(self, monkeypatch):
        register_provider("fake", FakeProvider)
        monkeypatch.setenv("LLM_PROVIDER", "fake")
        try:
            assert isinstance(get_provider(), FakeProvider)
        finally:
            unregister_provider("fake")

    def test_default_is_mock(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        assert isinstance(get_provider(), MockProvider)


class TestProviderResponse:
    def test_total_tokens_and_dict(self):
        response = ProviderResponse(
            content="x",
            model="m",
            prompt_tokens=10,
            completion_tokens=5,
            latency_seconds=0.2,
        )
        assert response.total_tokens == 15
        assert response.to_dict()["total_tokens"] == 15
        assert response.to_dict()["content"] == "x"

    def test_total_tokens_none_without_usage(self):
        assert ProviderResponse(content="x").total_tokens is None


class TestErrorHierarchy:
    def test_subclasses_of_provider_error(self):
        for cls in (
            ProviderConfigurationError,
            UnknownProviderError,
            ProviderAuthenticationError,
            ProviderRateLimitError,
            ProviderUnavailableError,
            ProviderResponseError,
        ):
            assert issubclass(cls, ProviderError)


class TestProviderContract:
    @pytest.mark.parametrize("factory", [FakeProvider, MockProvider])
    def test_shared_contract(self, factory):
        provider = factory()
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]

        response = provider.chat(messages)
        assert isinstance(response, ProviderResponse)
        assert isinstance(response.content, str) and response.content

        chunks = list(provider.stream(messages))
        assert chunks and all(isinstance(chunk, str) for chunk in chunks)

        assert provider.complete(messages) == provider.chat(messages).content
        assert isinstance(provider.name, str) and provider.name
        assert provider.models

    def test_new_provider_needs_only_the_contract(self):
        provider = FakeProvider()
        assert provider.complete([{"role": "user", "content": "hi"}]) == "hello"


class TestOpenAIProvider:
    def test_missing_key_raises_configuration_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(ProviderConfigurationError):
            OpenAIProvider(api_key="").chat([{"role": "user", "content": "hi"}])

    def test_chat_returns_uniform_response(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None, stream=False, **kwargs):
            captured.update(url=url, headers=headers, payload=json)
            return FakeResponse(
                200,
                {
                    "model": "gpt-4o-mini",
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                },
            )

        monkeypatch.setattr("app.services.providers.openai.requests.post", fake_post)
        response = OpenAIProvider(api_key="k").chat([{"role": "user", "content": "hi"}])

        assert response.content == "hello"
        assert response.prompt_tokens == 11
        assert response.completion_tokens == 7
        assert response.total_tokens == 18
        assert response.model == "gpt-4o-mini"
        assert response.latency_seconds is not None
        assert captured["url"].endswith("/chat/completions")
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["payload"]["stream"] is False

    @pytest.mark.parametrize(
        "status,expected",
        [
            (401, ProviderAuthenticationError),
            (403, ProviderAuthenticationError),
            (429, ProviderRateLimitError),
            (500, ProviderUnavailableError),
            (400, ProviderResponseError),
        ],
    )
    def test_status_mapping(self, monkeypatch, status, expected):
        monkeypatch.setattr(
            "app.services.providers.openai.requests.post",
            lambda *a, **k: FakeResponse(status, {}),
        )
        with pytest.raises(expected):
            OpenAIProvider(api_key="k").chat([{"role": "user", "content": "hi"}])

    def test_stream_yields_deltas(self, monkeypatch):
        lines = [
            'data: {"choices":[{"delta":{"content":"he"}}]}',
            'data: {"choices":[{"delta":{"content":"llo"}}]}',
            "data: [DONE]",
        ]
        monkeypatch.setattr(
            "app.services.providers.openai.requests.post",
            lambda *a, **k: FakeResponse(200, {}, lines=lines),
        )
        chunks = list(OpenAIProvider(api_key="k").stream([{"role": "user", "content": "hi"}]))
        assert chunks == ["he", "llo"]


class TestAnthropicProvider:
    def test_missing_key_raises_configuration_error(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(ProviderConfigurationError):
            AnthropicProvider(api_key="").chat([{"role": "user", "content": "hi"}])

    def test_chat_splits_system_and_maps_usage(self, monkeypatch):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None, stream=False, **kwargs):
            captured.update(url=url, headers=headers, payload=json)
            return FakeResponse(
                200,
                {
                    "model": "claude-3-5-sonnet-latest",
                    "content": [{"type": "text", "text": "Hi "}, {"type": "text", "text": "there"}],
                    "usage": {"input_tokens": 9, "output_tokens": 4},
                },
            )

        monkeypatch.setattr("app.services.providers.anthropic.requests.post", fake_post)
        response = AnthropicProvider(api_key="k").chat(
            [{"role": "system", "content": "be nice"}, {"role": "user", "content": "hi"}]
        )

        assert response.content == "Hi there"
        assert response.prompt_tokens == 9
        assert response.completion_tokens == 4
        assert captured["url"].endswith("/messages")
        assert captured["headers"]["x-api-key"] == "k"
        assert captured["payload"]["system"] == "be nice"
        assert captured["payload"]["messages"] == [{"role": "user", "content": "hi"}]

    @pytest.mark.parametrize(
        "status,expected",
        [
            (401, ProviderAuthenticationError),
            (429, ProviderRateLimitError),
            (503, ProviderUnavailableError),
            (400, ProviderResponseError),
        ],
    )
    def test_status_mapping(self, monkeypatch, status, expected):
        monkeypatch.setattr(
            "app.services.providers.anthropic.requests.post",
            lambda *a, **k: FakeResponse(status, {}),
        )
        with pytest.raises(expected):
            AnthropicProvider(api_key="k").chat([{"role": "user", "content": "hi"}])

    def test_stream_yields_text_deltas(self, monkeypatch):
        lines = [
            'data: {"type":"content_block_delta","delta":{"text":"He"}}',
            'data: {"type":"content_block_delta","delta":{"text":"llo"}}',
            'data: {"type":"message_stop"}',
        ]
        monkeypatch.setattr(
            "app.services.providers.anthropic.requests.post",
            lambda *a, **k: FakeResponse(200, {}, lines=lines),
        )
        chunks = list(AnthropicProvider(api_key="k").stream([{"role": "user", "content": "hi"}]))
        assert chunks == ["He", "llo"]


class TestCompatibilityShim:
    def test_llm_provider_error_alias(self):
        from app.services.llm import LLMProviderError

        assert LLMProviderError is ProviderError

    def test_llm_get_provider_defaults_to_mock(self, monkeypatch):
        from app.services import llm

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        assert isinstance(llm.get_provider(), MockProvider)
