"""Tests for bounded webhook retry policy (#670)."""

from __future__ import annotations

import pytest

from app.services.webhook_retry import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    DeliveryAttempt,
    RetryPolicy,
    deliver_with_retry,
)


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_retries_until_success_and_records_each_attempt():
    responses = [FakeResponse(500), FakeResponse(503), FakeResponse(204)]
    sleeps: list[float] = []
    recorded: list[DeliveryAttempt] = []

    def send():
        return responses.pop(0)

    result = deliver_with_retry(
        send,
        policy=RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=10.0, total_delay_cap=100.0),
        sleep=sleeps.append,
        jitter=lambda: 1.0,
        on_attempt=recorded.append,
    )

    assert result.success is True
    assert result.response_code == 204
    assert [attempt.status for attempt in result.attempts] == [
        STATUS_FAILURE,
        STATUS_FAILURE,
        STATUS_SUCCESS,
    ]
    assert [attempt.response_code for attempt in result.attempts] == [500, 503, 204]
    assert sleeps == [1.0, 2.0]
    assert recorded == result.attempts


def test_stops_after_the_attempt_cap():
    sleeps: list[float] = []

    result = deliver_with_retry(
        lambda: FakeResponse(500),
        policy=RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=10.0, total_delay_cap=100.0),
        sleep=sleeps.append,
        jitter=lambda: 1.0,
    )

    assert result.success is False
    assert len(result.attempts) == 3
    assert result.response_code == 500
    assert result.error == "HTTP 500"
    assert sleeps == [1.0, 2.0]


def test_total_delay_cap_stops_further_attempts():
    sleeps: list[float] = []

    result = deliver_with_retry(
        lambda: FakeResponse(503),
        policy=RetryPolicy(max_attempts=10, base_delay=4.0, max_delay=100.0, total_delay_cap=5.0),
        sleep=sleeps.append,
        jitter=lambda: 1.0,
    )

    # First delay (4s) fits under the 5s cap; the next (8s) would exceed it.
    assert sleeps == [4.0]
    assert len(result.attempts) == 2
    assert result.success is False


def test_exceptions_are_recorded_without_leaking_messages():
    calls = {"count": 0}

    def send():
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionError("https://example.com/hook?token=secret")
        return FakeResponse(200)

    result = deliver_with_retry(
        send,
        policy=RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0, total_delay_cap=0.0),
        sleep=lambda _delay: None,
        jitter=lambda: 1.0,
    )

    assert result.success is True
    failed = result.attempts[0]
    assert failed.status == STATUS_FAILURE
    assert failed.response_code is None
    assert failed.error == "ConnectionError"
    assert "secret" not in (failed.error or "")


def test_delay_is_capped_and_jittered():
    policy = RetryPolicy(base_delay=1.0, max_delay=4.0)

    assert policy.delay_for(1, jitter=lambda: 1.0) == 1.0
    assert policy.delay_for(3, jitter=lambda: 1.0) == 4.0
    assert policy.delay_for(10, jitter=lambda: 1.0) == 4.0
    assert policy.delay_for(3, jitter=lambda: 0.5) == 2.0
    assert policy.delay_for(0, jitter=lambda: 1.0) == 0.0


def test_policy_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=-1)
