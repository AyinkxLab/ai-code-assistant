"""Cost accounting from the configurable provider/model price table (#367).

Costs are *estimates*: token counts come from the provider (or are estimated by
the caller), and the price table is an operator-verified snapshot. Every value
returned here is labelled so callers never present an estimate as an exact
charge. Unknown provider/model combinations return ``amount=None`` rather than
inventing zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import current_app, has_app_context

from app.services import pricing
from app.services.currency import get_currency_config


@dataclass(frozen=True)
class UsageCost:
    """A labelled cost estimate for one usage event."""

    provider: str | None
    model: str | None
    amount: float | None
    currency: dict[str, Any]
    cost_is_estimate: bool
    priced: bool
    token_source: str
    disclaimer: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly payload."""
        return {
            "provider": self.provider,
            "model": self.model,
            "estimated_cost_usd": self.amount,
            "estimated_cost": self.amount,
            "currency": self.currency,
            "cost_is_estimate": self.cost_is_estimate,
            "priced": self.priced,
            "token_source": self.token_source,
            "disclaimer": self.disclaimer,
        }


def compute_usage_cost(
    provider: str | None,
    model: str | None,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reported_tokens: bool = False,
    workspace_id: Any = None,
    table: Any = None,
) -> UsageCost:
    """Compute a labelled cost estimate from the active price table.

    ``reported_tokens`` records whether the token counts came from the provider
    or were estimated locally; the price itself is always an estimate unless the
    table marks the model as exact (the offline mock provider).
    """
    price = pricing.get_model_price(provider, model, table=table, workspace_id=workspace_id)
    amount = pricing.estimate_cost_usd(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        table=table,
        workspace_id=workspace_id,
    )
    return UsageCost(
        provider=provider,
        model=model,
        amount=amount,
        currency=get_currency_config(),
        cost_is_estimate=True if price is None else price.estimated,
        priced=price is not None,
        token_source="reported" if reported_tokens else "estimated",
        disclaimer=pricing.ESTIMATE_DISCLAIMER,
    )


def compute_from_record(record: Any) -> UsageCost:
    """Compute the labelled cost for a ``UsageRecord``-shaped object."""
    return compute_usage_cost(
        getattr(record, "provider", None),
        getattr(record, "model", None),
        prompt_tokens=int(getattr(record, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(record, "completion_tokens", 0) or 0),
        reported_tokens=not bool(getattr(record, "tokens_estimated", True)),
        workspace_id=getattr(record, "workspace_id", None),
    )


def estimate_preflight_cost(
    tokens: int,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> float:
    """Estimate pre-flight cost for ``tokens``.

    When ``LLM_MODEL`` is configured, the provider/model price table is used.
    Otherwise the legacy ``LLM_ESTIMATE_USD_PER_1K_TOKENS`` rate is retained so
    existing deployments keep their configured figure.
    """
    config = current_app.config if has_app_context() else {}
    provider = provider or config.get("LLM_PROVIDER") or "mock"
    model = model or config.get("LLM_MODEL")
    if model:
        amount = pricing.estimate_cost_usd(provider, model, prompt_tokens=tokens)
        if amount is not None:
            return amount
    rate = float(config.get("LLM_ESTIMATE_USD_PER_1K_TOKENS", 0.0) or 0.0)
    return round(tokens / 1000 * rate, 6)


__all__ = [
    "UsageCost",
    "compute_from_record",
    "compute_usage_cost",
    "estimate_preflight_cost",
]
