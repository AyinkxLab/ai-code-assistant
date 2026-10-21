"""Tests for LLM_PRICE_TABLE validation and safe degradation (#367/#438)."""

from __future__ import annotations

import pytest

from app.services import pricing


def test_valid_overlay_changes_lookup_and_keeps_defaults():
    table = pricing.parse_price_table(
        '{"openai/gpt-4o": {"input_per_1k_usd": 9, "output_per_1k_usd": 18}}'
    )

    overridden = pricing.get_model_price("openai", "gpt-4o", table=table)
    default = pricing.get_model_price("openai", "gpt-4o-mini", table=table)

    assert overridden is not None
    assert overridden.input_per_1k_usd == 9
    assert overridden.output_per_1k_usd == 18
    assert default is not None
    assert default.input_per_1k_usd == 0.00015


def test_empty_config_returns_the_shipped_table():
    table = pricing.parse_price_table("")
    assert pricing.get_model_price("mock", "mock-1", table=table) is not None


@pytest.mark.parametrize(
    "text, message",
    [
        ("{not json", "invalid JSON"),
        ("[]", "must be a JSON object"),
        ('{"gpt-4o": {"input_per_1k_usd": 1, "output_per_1k_usd": 1}}', "provider/model"),
        ('{"openai/gpt-4o": 1}', "must be an object"),
        (
            '{"openai/gpt-4o": {"input_per_1k_usd": "free", "output_per_1k_usd": 1}}',
            "must be a number",
        ),
        (
            '{"openai/gpt-4o": {"input_per_1k_usd": -1, "output_per_1k_usd": 1}}',
            "non-negative",
        ),
        (
            '{"openai/gpt-4o": {"input_per_1k_usd": NaN, "output_per_1k_usd": 1}}',
            "non-negative",
        ),
    ],
)
def test_malformed_tables_raise_actionable_errors(text, message):
    with pytest.raises(pricing.PriceTableConfigError, match=message):
        pricing.parse_price_table(text)


def test_configure_price_table_activates_valid_overrides(app):
    app.config["LLM_PRICE_TABLE"] = (
        '{"openai/gpt-4o": {"input_per_1k_usd": 9, "output_per_1k_usd": 18}}'
    )

    pricing.configure_price_table(app)

    assert app.config["PRICE_TABLE_ERROR"] is None
    price = pricing.get_model_price("openai", "gpt-4o")
    assert price is not None and price.input_per_1k_usd == 9
    assert pricing.estimate_cost_usd("openai", "gpt-4o", prompt_tokens=1000) == 9.0


def test_invalid_config_degrades_to_defaults_without_breaking_requests(app, client):
    app.config["LLM_PRICE_TABLE"] = "{ definitely not json"

    pricing.configure_price_table(app)

    assert app.config["PRICE_TABLE_ERROR"]
    price = pricing.get_model_price("openai", "gpt-4o")
    assert price is not None
    assert price.input_per_1k_usd == 0.0025
    # The application still serves usage endpoints.
    assert client.get("/api/usage/breakdown").status_code in (401, 200)
