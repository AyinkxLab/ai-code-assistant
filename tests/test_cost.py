"""Tests for labelled cost accounting (#367)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services import cost, project_analysis


def test_known_model_cost_is_labelled_as_an_estimate(app):
    result = cost.compute_usage_cost("openai", "gpt-4o", prompt_tokens=1000, completion_tokens=500)

    assert result.amount == 0.0075
    assert result.priced is True
    assert result.cost_is_estimate is True
    assert result.token_source == "estimated"
    assert result.currency["code"] == "USD"
    assert "estimate" in result.disclaimer.lower()


def test_provider_reported_tokens_are_marked_reported(app):
    result = cost.compute_usage_cost(
        "openai",
        "gpt-4o-mini",
        prompt_tokens=1000,
        completion_tokens=0,
        reported_tokens=True,
    )

    assert result.token_source == "reported"
    assert result.amount == 0.00015
    assert result.cost_is_estimate is True


def test_mock_provider_is_exact_zero(app):
    result = cost.compute_usage_cost("mock", "mock-1", prompt_tokens=10_000)

    assert result.amount == 0.0
    assert result.cost_is_estimate is False
    assert result.priced is True


def test_unknown_model_is_not_presented_as_free(app):
    result = cost.compute_usage_cost("no-such-provider", "no-such-model", prompt_tokens=1000)

    assert result.amount is None
    assert result.priced is False
    assert result.cost_is_estimate is True


def test_compute_from_record_uses_reported_flag(app):
    record = SimpleNamespace(
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=1000,
        completion_tokens=1000,
        tokens_estimated=False,
        workspace_id=None,
    )

    result = cost.compute_from_record(record)

    assert result.token_source == "reported"
    assert result.amount == 0.00075


def test_preflight_uses_the_price_table_when_a_model_is_configured(app):
    app.config["LLM_PROVIDER"] = "openai"
    app.config["LLM_MODEL"] = "gpt-4o"
    app.config["LLM_ESTIMATE_USD_PER_1K_TOKENS"] = 999.0

    assert cost.estimate_preflight_cost(1000) == 0.0025


def test_preflight_keeps_the_legacy_rate_without_a_model(app):
    app.config["LLM_PROVIDER"] = "openai"
    app.config["LLM_MODEL"] = ""
    app.config["LLM_ESTIMATE_USD_PER_1K_TOKENS"] = 2.0

    assert cost.estimate_preflight_cost(1000) == 2.0


def test_project_analysis_preflight_uses_cost_service(app):
    assert project_analysis._estimate_cost(1000) == 0.002
