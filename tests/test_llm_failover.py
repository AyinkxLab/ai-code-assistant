"""Tests for graceful LLM provider failover (issue #39).

The unit tests drive :class:`FailoverProvider` with fake providers so ordering,
error classification, the no-replay invariant, and diagnostics are asserted
without any network access. The route tests then confirm the chain is wired
into the real SSE emitters, including the all-failed ``error`` event.
"""

import json
import logging

import pytest

from app.services import chat_audit
from app.services.provider_config import (
    default_provider_chain,
    provider_chain_status,
    resolve_provider_chain,
    routing_rules,
)
from app.services.providers import (
    MockProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderFailoverError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    build_failover_chain,
    classify_error,
    is_transient_error,
    register_provider,
    unregister_provider,
)
from app.services.providers.failover import FailoverProvider
from app.services.providers.retry import RetryingProvider


class FakeProvider:
    """A provider whose chat/stream behaviour is scripted per test."""

    requires_key = False
    supports_vision = False

    def __init__(
        self,
        name,
        *,
        models=("m",),
        chat_error=None,
        chat_response=None,
        stream_chunks=(),
        stream_error=None,
    ):
        self.name = name
        self.models = tuple(models)
        self.chat_error = chat_error
        self.chat_response = chat_response
        self.stream_chunks = tuple(stream_chunks)
        self.stream_error = stream_error
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, messages, *, model=None, params=None):
        self.chat_calls += 1
        if self.chat_error is not None:
            raise self.chat_error
        return ProviderResponse(
            content=self.chat_response or f"{self.name}-reply",
            model=model or self.models[0],
        )

    def stream(self, messages, *, model=None, params=None):
        self.stream_calls += 1
        for chunk in self.stream_chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


def _chain(*providers, wrap=lambda provider: provider, **kwargs):
    by_name = {provider.name: provider for provider in providers}
    return build_failover_chain(
        [provider.name for provider in providers],
        build=lambda name: by_name[name],
        wrap=wrap,
        **kwargs,
    )


def _fast_retry(provider):
    return RetryingProvider(provider, max_retries=1, sleep=lambda _delay: None, jitter=lambda: 0.0)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def events(self) -> list[dict]:
        formatter = chat_audit.JsonFormatter()
        return [json.loads(formatter.format(record)) for record in self.records]


@pytest.fixture()
def capture():
    handler = _Capture()
    chat_audit.LOGGER.addHandler(handler)
    chat_audit.LOGGER.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        chat_audit.LOGGER.removeHandler(handler)


def _events_by_name(capture, name):
    return [event for event in capture.events() if event.get("event") == name]


class TestFailoverOrdering:
    def test_second_provider_serves_when_first_is_transient(self):
        first = FakeProvider(
            "first", chat_error=ProviderUnavailableError("down", provider="first")
        )
        second = FakeProvider("second", chat_response="second-reply")

        chain = _chain(first, second)
        response = chain.chat([{"role": "user", "content": "hi"}])

        assert response.content == "second-reply"
        assert first.chat_calls == 1
        assert second.chat_calls == 1
        # The serving provider is reported for logs/tests, not the user.
        assert chain.served_provider == "second"
        diagnostics = chain.diagnostics()
        assert diagnostics["served_provider"] == "second"
        assert diagnostics["failed"] is False
        assert [attempt["provider"] for attempt in diagnostics["attempts"]] == [
            "first",
            "second",
        ]

    def test_retries_exhaust_before_failover(self):
        flaky = FakeProvider(
            "flaky", chat_error=ProviderUnavailableError("blip", provider="flaky")
        )
        fallback = FakeProvider("fallback")

        chain = _chain(flaky, fallback, wrap=_fast_retry)
        response = chain.chat([{"role": "user", "content": "hi"}])

        assert response.content == "fallback-reply"
        # Each provider retries within itself first; only an exhausted provider
        # hands over to the next one.
        assert flaky.chat_calls == 2
        assert fallback.chat_calls == 1
        assert chain.served_provider == "fallback"

    def test_stream_fails_over_before_first_chunk(self):
        first = FakeProvider(
            "first",
            stream_error=ProviderUnavailableError("down", provider="first"),
        )
        second = FakeProvider("second", stream_chunks=("b1", "b2"))

        chain = _chain(first, second)
        chunks = list(chain.stream([{"role": "user", "content": "hi"}]))

        assert chunks == ["b1", "b2"]
        assert first.stream_calls == 1
        assert second.stream_calls == 1
        assert chain.served_provider == "second"

    def test_chain_metadata_merges_providers(self):
        first = FakeProvider("first", models=("m1",))
        second = FakeProvider("second", models=("m1", "m2"))

        chain = _chain(first, second)

        assert isinstance(chain, FailoverProvider)
        assert chain.name == "first+second"
        assert chain.chain == ("first", "second")
        assert chain.models == ("m1", "m2")


class TestErrorClassification:
    @pytest.mark.parametrize(
        "error,expected",
        [
            (ProviderRateLimitError("x"), "rate_limit"),
            (ProviderUnavailableError("x"), "unavailable"),
            (ProviderAuthenticationError("x"), "authentication"),
            (ProviderConfigurationError("x"), "configuration"),
            (ProviderResponseError("x"), "response"),
            (ProviderError("x"), "provider"),
            (ValueError("x"), "unknown"),
        ],
    )
    def test_classify_error(self, error, expected):
        assert classify_error(error) == expected

    def test_non_transient_fails_fast_without_failover(self):
        first = FakeProvider(
            "first",
            chat_error=ProviderAuthenticationError("bad key", provider="first"),
        )
        second = FakeProvider("second")

        chain = _chain(first, second, wrap=_fast_retry)
        with pytest.raises(ProviderAuthenticationError):
            chain.chat([{"role": "user", "content": "hi"}])

        assert first.chat_calls == 1  # auth errors are not retried
        assert second.chat_calls == 0  # and never failed over to
        assert chain.served_provider is None

    def test_non_transient_stream_error_does_not_fail_over(self):
        first = FakeProvider(
            "first",
            stream_error=ProviderResponseError("malformed", provider="first"),
        )
        second = FakeProvider("second", stream_chunks=("b",))

        chain = _chain(first, second)
        with pytest.raises(ProviderResponseError):
            list(chain.stream([{"role": "user", "content": "hi"}]))

        assert second.stream_calls == 0
        assert is_transient_error(ProviderResponseError("x")) is False


class TestNoReplayAfterPartialOutput:
    def test_partial_stream_is_not_replayed_on_failure(self):
        first = FakeProvider(
            "first",
            stream_chunks=("partial",),
            stream_error=ProviderUnavailableError("dropped", provider="first"),
        )
        second = FakeProvider("second", stream_chunks=("second-answer",))

        chain = _chain(first, second)
        received = []
        with pytest.raises(ProviderUnavailableError):
            for chunk in chain.stream([{"role": "user", "content": "hi"}]):
                received.append(chunk)

        # The client already rendered "partial"; a second provider would
        # duplicate the answer, so the error is surfaced instead.
        assert received == ["partial"]
        assert second.stream_calls == 0
        assert chain.served_provider is None
        assert chain.partial_provider == "first"
        assert chain.diagnostics()["attempts"][0]["partial"] is True


class TestAllProvidersFailed:
    def test_chat_raises_explainable_error_for_the_whole_chain(self):
        first = FakeProvider(
            "first", chat_error=ProviderUnavailableError("first down", provider="first")
        )
        second = FakeProvider(
            "second", chat_error=ProviderRateLimitError("second busy", provider="second")
        )

        chain = _chain(first, second)
        with pytest.raises(ProviderFailoverError) as excinfo:
            chain.chat([{"role": "user", "content": "hi"}])

        message = str(excinfo.value)
        assert "All configured LLM providers failed" in message
        assert "first" in message and "second" in message
        assert chain.request_id in message
        # The aggregate error is terminal, never retried as transient.
        assert is_transient_error(excinfo.value) is False
        assert [attempt.category for attempt in excinfo.value.attempts] == [
            "unavailable",
            "rate_limit",
        ]

    def test_single_provider_chain_preserves_original_error(self):
        only = FakeProvider(
            "only", chat_error=ProviderUnavailableError("only down", provider="only")
        )

        chain = _chain(only)
        with pytest.raises(ProviderUnavailableError) as excinfo:
            chain.chat([{"role": "user", "content": "hi"}])

        assert not isinstance(excinfo.value, ProviderFailoverError)
        assert "only down" in str(excinfo.value)

    def test_stream_all_failed_raises_aggregate(self):
        first = FakeProvider(
            "first", stream_error=ProviderUnavailableError("first down", provider="first")
        )
        second = FakeProvider(
            "second", stream_error=ProviderUnavailableError("second down", provider="second")
        )

        chain = _chain(first, second)
        with pytest.raises(ProviderFailoverError):
            list(chain.stream([{"role": "user", "content": "hi"}]))


class TestAttemptDiagnosticsLogging:
    def test_one_event_per_attempt_with_a_shared_request_id(self, capture):
        first = FakeProvider(
            "first", chat_error=ProviderUnavailableError("down", provider="first")
        )
        second = FakeProvider("second")

        chain = _chain(first, second)
        chain.chat([{"role": "user", "content": "hi"}])

        events = _events_by_name(capture, "chat.provider_attempt")
        assert len(events) == 2
        assert {event["request_id"] for event in events} == {chain.request_id}
        assert chain.request_id
        assert [event["attempt"] for event in events] == [1, 2]
        assert [event["provider"] for event in events] == ["first", "second"]
        assert events[0]["status"] == "error"
        assert events[0]["error_category"] == "unavailable"
        assert events[0]["transient"] is True
        assert events[0]["served"] is False
        assert events[1]["status"] == "success"
        assert events[1]["served"] is True
        assert events[1]["chain"] == "first,second"

    def test_explicit_request_id_is_threaded_through_attempts(self, capture):
        first = FakeProvider(
            "first", stream_error=ProviderUnavailableError("down", provider="first")
        )
        second = FakeProvider("second", stream_chunks=("ok",))

        chain = _chain(first, second, request_id="req-42")
        list(chain.stream([{"role": "user", "content": "hi"}]))

        events = _events_by_name(capture, "chat.provider_attempt")
        assert {event["request_id"] for event in events} == {"req-42"}
        assert all(event["stream"] is True for event in events)


class TestChainConfiguration:
    def test_single_provider_is_the_backwards_compatible_default(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER_CHAIN", raising=False)
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        assert default_provider_chain() == ("openai",)
        assert resolve_provider_chain(primary=None) == ("openai",)

    def test_ordered_chain_from_env(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_CHAIN", "openai, anthropic")
        monkeypatch.delenv("LLM_ROUTING_RULES", raising=False)

        assert resolve_provider_chain(primary=None) == ("openai", "anthropic")
        # The conversation's explicit provider is always tried first.
        assert resolve_provider_chain(primary="mock") == ("mock", "openai", "anthropic")

    def test_routing_rules_select_a_chain(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER_CHAIN", "openai,anthropic")
        monkeypatch.setenv("LLM_ROUTING_RULES", "vision=anthropic,mock;cheap=mock")

        assert routing_rules()["vision"] == ("anthropic", "mock")
        assert resolve_provider_chain(route="vision", primary=None) == ("anthropic", "mock")
        assert resolve_provider_chain(route="vision", primary="openai") == (
            "openai",
            "anthropic",
            "mock",
        )
        # An unknown route falls back to the default chain.
        assert resolve_provider_chain(route="other", primary=None) == ("openai", "anthropic")

    def test_malformed_routing_rules_are_ignored(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTING_RULES", "=openai;vision=;broken;cheap=mock")

        assert routing_rules() == {"cheap": ("mock",)}

    def test_build_failover_chain_wraps_each_provider_in_retries(self):
        chain = build_failover_chain(["mock"], build=lambda name: MockProvider())

        assert isinstance(chain.providers[0], RetryingProvider)
        assert chain.name == "mock"
        assert chain.served_provider is None

    def test_empty_chain_is_a_configuration_error(self):
        with pytest.raises(ProviderConfigurationError):
            build_failover_chain([], build=lambda name: MockProvider())


class TestChainStatus:
    def test_configured_when_any_fallback_is_ready(self, monkeypatch):
        register_provider("keyless", lambda: FakeProvider("keyless"))
        try:
            monkeypatch.setenv("LLM_PROVIDER", "openai")
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            monkeypatch.setenv("LLM_PROVIDER_CHAIN", "openai,keyless")

            status = provider_chain_status(None)

            assert status["configured"] is True
            assert status["chain"] == ["openai", "keyless"]
            assert status["configured_providers"] == ["keyless"]
        finally:
            unregister_provider("keyless")

    def test_unconfigured_chain_members_are_skipped(self, monkeypatch):
        import app.chat.routes as chat_routes

        class KeyedProvider(FakeProvider):
            requires_key = True

        register_provider("needs_key", lambda: KeyedProvider("needs_key"))
        register_provider("keyless", lambda: FakeProvider("keyless"))
        try:
            monkeypatch.setenv("LLM_PROVIDER_CHAIN", "needs_key,keyless")
            monkeypatch.delenv("LLM_ROUTING_RULES", raising=False)
            monkeypatch.setattr(
                chat_routes, "build_provider", lambda user, name=None: FakeProvider(name)
            )

            # A provider that needs a key but has none is not part of the usable
            # chain, so the keyless fallback is the one that runs.
            chain = chat_routes.build_provider_chain(None)

            assert chain.chain == ("keyless",)
        finally:
            unregister_provider("needs_key")
            unregister_provider("keyless")

    def test_unconfigured_when_every_provider_lacks_a_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("LLM_PROVIDER_CHAIN", raising=False)

        status = provider_chain_status(None)

        assert status["configured"] is False
        assert status["provider"] == "openai"


# --------------------------------------------------------------------------
# Route integration: the SSE emitters must actually use the chain.
# --------------------------------------------------------------------------


class RouteFailingProvider:
    requires_key = False
    supports_vision = False
    models = ("m",)

    def __init__(self, name):
        self.name = name

    def chat(self, messages, *, model=None, params=None):
        raise ProviderUnavailableError(f"{self.name} down", provider=self.name)

    def stream(self, messages, *, model=None, params=None):
        raise ProviderUnavailableError(f"{self.name} down", provider=self.name)


class RouteServingProvider:
    requires_key = False
    supports_vision = False
    models = ("m",)

    def __init__(self, name):
        self.name = name

    def chat(self, messages, *, model=None, params=None):
        return ProviderResponse(content=f"served by {self.name}", model=self.models[0])

    def stream(self, messages, *, model=None, params=None):
        yield f"served by {self.name}"


def _register(client, username="tester", email="tester@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


def _create_conversation(client):
    response = client.post(
        "/chat/conversations", json={}, headers={"X-CSRFToken": "ignored"}
    )
    assert response.status_code == 201
    return response.get_json()


class TestStreamFailoverIntegration:
    def test_stream_fails_over_to_the_next_provider(self, client, db, monkeypatch):
        import app.chat.routes as chat_routes

        register_provider("faila", lambda: RouteFailingProvider("faila"))
        register_provider("server", lambda: RouteServingProvider("server"))
        try:
            monkeypatch.setenv("LLM_PROVIDER_CHAIN", "faila,server")
            monkeypatch.delenv("LLM_ROUTING_RULES", raising=False)
            monkeypatch.setattr(chat_routes, "RetryingProvider", lambda inner, **kwargs: inner)
            calls = []

            def build(user, name=None):
                calls.append(name)
                if name == "faila":
                    return RouteFailingProvider("faila")
                return RouteServingProvider("server")

            monkeypatch.setattr(chat_routes, "build_provider", build)

            _register(client)
            conversation = _create_conversation(client)
            response = client.post(
                f"/chat/conversations/{conversation['id']}/stream",
                json={"content": "hi"},
                headers={"X-CSRFToken": "ignored"},
            )

            data = response.get_data(as_text=True)
            assert '"type": "token"' in data
            assert "served by server" in data
            assert '"type": "done"' in data
            assert calls == ["faila", "server"]
        finally:
            unregister_provider("faila")
            unregister_provider("server")

    def test_stream_all_providers_failed_emits_error_event(self, client, db, monkeypatch):
        import app.chat.routes as chat_routes

        register_provider("faila", lambda: RouteFailingProvider("faila"))
        register_provider("failb", lambda: RouteFailingProvider("failb"))
        try:
            monkeypatch.setenv("LLM_PROVIDER_CHAIN", "faila,failb")
            monkeypatch.setattr(chat_routes, "RetryingProvider", lambda inner, **kwargs: inner)

            _register(client)
            conversation = _create_conversation(client)
            response = client.post(
                f"/chat/conversations/{conversation['id']}/stream",
                json={"content": "hi"},
                headers={"X-CSRFToken": "ignored"},
            )

            data = response.get_data(as_text=True)
            assert '"type": "error"' in data
            assert "All configured LLM providers failed" in data
            assert "faila" in data and "failb" in data
        finally:
            unregister_provider("faila")
            unregister_provider("failb")
