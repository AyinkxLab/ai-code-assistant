"""Tests for retry with exponential backoff (issue #29).

A fake provider fails a configurable number of times with a transient error
before succeeding, so retry counts, backoff delays, fail-fast behaviour, and
stream establishment are asserted without any network access.
"""

import pytest

from app.services.providers import (
    MockProvider,
    ProviderAuthenticationError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    get_retrying_provider,
)
from app.services.providers.retry import RetryingProvider, is_transient_error


class FlakyProvider:
    """Fails ``failures`` times with ``error``, then succeeds."""

    name = "flaky"
    models = ("flaky-1",)

    def __init__(self, failures=0, error=ProviderUnavailableError):
        self.failures = failures
        self.error = error
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, messages, *, model=None, params=None):
        self.chat_calls += 1
        if self.chat_calls <= self.failures:
            raise self.error("transient failure", provider=self.name)
        return ProviderResponse(content="ok", model=model or self.models[0])

    def stream(self, messages, *, model=None, params=None):
        self.stream_calls += 1
        if self.stream_calls <= self.failures:
            raise self.error("transient failure", provider=self.name)
        yield "ok"


class RecordingSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, delay):
        self.delays.append(delay)


def _wrap(provider, sleep=None, **kwargs):
    return RetryingProvider(
        provider,
        sleep=sleep or RecordingSleep(),
        jitter=lambda: 1.0,
        **kwargs,
    )


class TestChatRetries:
    def test_retries_until_success(self):
        provider = FlakyProvider(failures=2)
        sleep = RecordingSleep()
        result = _wrap(provider, sleep=sleep).chat([{"role": "user", "content": "hi"}])

        assert result.content == "ok"
        assert provider.chat_calls == 3
        assert len(sleep.delays) == 2

    def test_backoff_is_exponential(self):
        provider = FlakyProvider(failures=3)
        sleep = RecordingSleep()
        _wrap(provider, sleep=sleep, base_delay=0.1, max_delay=10.0).chat(
            [{"role": "user", "content": "hi"}]
        )
        assert sleep.delays == pytest.approx([0.1, 0.2, 0.4])

    def test_gives_up_after_max_retries(self):
        provider = FlakyProvider(failures=5)
        with pytest.raises(ProviderUnavailableError):
            _wrap(provider, max_retries=2).chat([{"role": "user", "content": "hi"}])
        assert provider.chat_calls == 3  # initial attempt + 2 retries

    def test_max_retries_zero_means_no_retry(self):
        provider = FlakyProvider(failures=1)
        with pytest.raises(ProviderUnavailableError):
            _wrap(provider, max_retries=0).chat([{"role": "user", "content": "hi"}])
        assert provider.chat_calls == 1

    def test_rate_limit_is_transient(self):
        provider = FlakyProvider(failures=1, error=ProviderRateLimitError)
        result = _wrap(provider, max_retries=3).chat([{"role": "user", "content": "hi"}])
        assert result.content == "ok"
        assert provider.chat_calls == 2

    @pytest.mark.parametrize(
        "error",
        [ProviderAuthenticationError, ProviderResponseError],
    )
    def test_non_transient_errors_fail_fast(self, error):
        provider = FlakyProvider(failures=1, error=error)
        sleep = RecordingSleep()
        with pytest.raises(error):
            _wrap(provider, sleep=sleep, max_retries=3).chat([{"role": "user", "content": "hi"}])
        assert provider.chat_calls == 1
        assert sleep.delays == []

    def test_complete_uses_retries(self):
        provider = FlakyProvider(failures=1)
        assert _wrap(provider).complete([{"role": "user", "content": "hi"}]) == "ok"
        assert provider.chat_calls == 2


class TestStreamRetries:
    def test_retries_before_first_chunk(self):
        provider = FlakyProvider(failures=1)
        chunks = list(_wrap(provider, max_retries=3).stream([{"role": "user", "content": "hi"}]))
        assert chunks == ["ok"]
        assert provider.stream_calls == 2

    def test_stream_retries_twice_then_succeeds(self):
        provider = FlakyProvider(failures=2)
        chunks = list(_wrap(provider, max_retries=3).stream([{"role": "user", "content": "hi"}]))
        assert chunks == ["ok"]
        assert provider.stream_calls == 3

    def test_exhausted_stream_raises(self):
        provider = FlakyProvider(failures=5)
        with pytest.raises(ProviderUnavailableError):
            list(_wrap(provider, max_retries=1).stream([{"role": "user", "content": "hi"}]))
        assert provider.stream_calls == 2


class TestDelayPolicy:
    def test_delay_caps_at_max(self):
        wrapper = _wrap(FlakyProvider(), base_delay=1.0, max_delay=2.5)
        assert wrapper.retry_delay(1) == pytest.approx(1.0)
        assert wrapper.retry_delay(2) == pytest.approx(2.0)
        assert wrapper.retry_delay(3) == pytest.approx(2.5)
        assert wrapper.retry_delay(10) == pytest.approx(2.5)

    def test_jitter_scales_delay(self):
        wrapper = RetryingProvider(
            FlakyProvider(), base_delay=1.0, max_delay=10.0, jitter=lambda: 0.25
        )
        assert wrapper.retry_delay(1) == pytest.approx(0.25)
        assert wrapper.retry_delay(2) == pytest.approx(0.5)

    def test_max_retries_from_env(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_RETRIES", "5")
        assert RetryingProvider(FlakyProvider()).max_retries == 5

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_RETRIES", "not-a-number")
        assert RetryingProvider(FlakyProvider()).max_retries == 3


class TestRetryingProviderContract:
    def test_delegates_metadata(self):
        wrapper = RetryingProvider(MockProvider())
        assert wrapper.name == "mock"
        assert wrapper.models == MockProvider.models

    def test_get_retrying_provider_wraps_selected_provider(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        wrapper = get_retrying_provider()
        assert isinstance(wrapper, RetryingProvider)
        assert isinstance(wrapper.provider, MockProvider)

    def test_is_transient_error(self):
        assert is_transient_error(ProviderUnavailableError("x"))
        assert is_transient_error(ProviderRateLimitError("x"))
        assert not is_transient_error(ProviderAuthenticationError("x"))
