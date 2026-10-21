"""Default LLM price table for common providers and models (issue #439).

The prices here are **estimates**, expressed in USD per 1,000 tokens, for
turning token counts into rough cost figures (pre-flight estimates, dashboards,
budgets). They are not a billing source of truth: vendors change prices and
introduce dated model snapshots, so operators must verify the numbers against
their provider contract before relying on them.

The offline ``mock`` provider is the one entry that is not an estimate: it
never calls a vendor and therefore always costs zero.

Lookups are provider/model exact matches after normalizing case and
surrounding whitespace. A ``*`` wildcard may be used for the provider and/or
model so an operator table can define a fallback rate; an exact entry always
takes precedence over a wildcard. An unknown provider or model returns ``None``
rather than guessing, so callers can distinguish "free" from "we do not know".

Token counts passed to :func:`estimate_cost_usd` are clamped to
:data:`MAX_TOKEN_COUNT` so an implausibly large (or malicious) count cannot
produce an infinite estimate. Negative counts are still rejected.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from flask import current_app, has_app_context

logger = logging.getLogger(__name__)

#: Environment/config key holding an optional JSON price-table overlay.
PRICE_TABLE_CONFIG_KEY = "LLM_PRICE_TABLE"

#: Wildcard key understood by :func:`get_model_price`.
WILDCARD = "*"

#: Upper bound applied to each token count before costing. A completion with
#: more tokens than this is almost certainly a bug or hostile input; clamping
#: keeps the estimate finite without silently accepting an unbounded value.
MAX_TOKEN_COUNT = 1_000_000_000

#: Shown next to the table in docs/UI wherever costs are displayed.
ESTIMATE_DISCLAIMER = (
    "Default prices are estimates in USD per 1,000 tokens. "
    "Verify them against your provider's current pricing before relying on them."
)

#: Canonical number of decimal places for every stored/aggregated USD cost (#444).
COST_PRECISION = 6

#: Quantum used by :func:`round_cost` (``0.000001`` USD).
_COST_QUANTUM = Decimal(1).scaleb(-COST_PRECISION)


def round_cost(value: float | int | Decimal | str | None) -> float | None:
    """Round a USD cost to :data:`COST_PRECISION` using half-up rounding.

    This is the one rounding rule the codebase uses for cost values. Rounding
    the decimal *representation* of the value (rather than the binary float)
    with ``ROUND_HALF_UP`` makes results deterministic and avoids the
    banker's-rounding surprises of the built-in :func:`round` at exact
    half-way boundaries. ``None`` is preserved so an unknown price stays
    unknown rather than becoming zero.
    """
    if value is None:
        return None
    try:
        decimal_value = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ValueError("Cost must be numeric.") from exc
    return float(decimal_value.quantize(_COST_QUANTUM, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ModelPrice:
    """Estimated input/output price for one provider model.

    Attributes:
        provider: Registered provider name (e.g. ``openai``).
        model: Model identifier as accepted by the provider.
        input_per_1k_usd: Estimated USD cost per 1,000 prompt tokens.
        output_per_1k_usd: Estimated USD cost per 1,000 completion tokens.
        estimated: ``False`` only for models with a known, guaranteed price
            (the offline mock provider). Everything else is a default estimate.
    """

    provider: str
    model: str
    input_per_1k_usd: float
    output_per_1k_usd: float
    estimated: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation, including the disclaimer."""
        data = asdict(self)
        data["disclaimer"] = ESTIMATE_DISCLAIMER
        return data


def _price(
    provider: str,
    model: str,
    input_per_1k_usd: float,
    output_per_1k_usd: float,
    *,
    estimated: bool = True,
) -> ModelPrice:
    return ModelPrice(
        provider=provider,
        model=model,
        input_per_1k_usd=input_per_1k_usd,
        output_per_1k_usd=output_per_1k_usd,
        estimated=estimated,
    )


#: Default, operator-verifiable estimates for the models registered by the
#: built-in providers. Keys are ``(provider, model)`` normalized to lowercase.
DEFAULT_PRICE_TABLE: dict[tuple[str, str], ModelPrice] = {
    # The offline provider is deterministic and free (not an estimate).
    ("mock", "mock-1"): _price("mock", "mock-1", 0.0, 0.0, estimated=False),
    # OpenAI defaults (see app/services/providers/openai.py).
    ("openai", "gpt-4o-mini"): _price("openai", "gpt-4o-mini", 0.00015, 0.0006),
    ("openai", "gpt-4o"): _price("openai", "gpt-4o", 0.0025, 0.01),
    ("openai", "gpt-4.1-mini"): _price("openai", "gpt-4.1-mini", 0.0004, 0.0016),
    ("openai", "gpt-4.1"): _price("openai", "gpt-4.1", 0.002, 0.008),
    # Anthropic defaults (see app/services/providers/anthropic.py).
    ("anthropic", "claude-3-5-sonnet-latest"): _price(
        "anthropic", "claude-3-5-sonnet-latest", 0.003, 0.015
    ),
    ("anthropic", "claude-3-5-haiku-latest"): _price(
        "anthropic", "claude-3-5-haiku-latest", 0.0008, 0.004
    ),
}


class PriceTableConfigError(ValueError):
    """The configured price table is malformed or contains invalid entries."""


def parse_price_table(text: str | None) -> dict[tuple[str, str], ModelPrice]:
    """Parse and validate ``LLM_PRICE_TABLE`` JSON.

    The document is an object keyed by ``"provider/model"``:

    ```json
    {
      "openai/gpt-4o": {"input_per_1k_usd": 0.0025, "output_per_1k_usd": 0.01},
      "mock/*": {"input_per_1k_usd": 0.0, "output_per_1k_usd": 0.0}
    }
    ```

    Entries are merged over :data:`DEFAULT_PRICE_TABLE`, so a partial table only
    overrides the keys it declares. Malformed JSON, non-object documents,
    missing/invalid keys, non-numeric, negative, or non-finite prices raise
    :class:`PriceTableConfigError` with an actionable message.
    """
    if text is None or not str(text).strip():
        return _normalized_table(DEFAULT_PRICE_TABLE)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PriceTableConfigError(f"invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PriceTableConfigError("price table must be a JSON object")

    parsed: dict[tuple[str, str], ModelPrice] = {}
    for key, value in document.items():
        if not isinstance(key, str) or "/" not in key:
            raise PriceTableConfigError(f"price table key {key!r} must be 'provider/model'")
        provider, model = key.split("/", 1)
        provider, model = provider.strip(), model.strip()
        if not provider or not model:
            raise PriceTableConfigError(
                f"price table key {key!r} must have a non-empty provider and model"
            )
        if not isinstance(value, dict):
            raise PriceTableConfigError(f"price table entry {key!r} must be an object")
        input_price = _validated_rate(key, "input_per_1k_usd", value.get("input_per_1k_usd"))
        output_price = _validated_rate(key, "output_per_1k_usd", value.get("output_per_1k_usd"))
        parsed[(provider.lower(), model.lower())] = _price(
            provider, model, input_price, output_price
        )

    merged = _normalized_table(DEFAULT_PRICE_TABLE)
    merged.update(parsed)
    return merged


def _validated_rate(key: str, field: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceTableConfigError(f"price table entry {key!r}.{field} must be a number")
    rate = float(value)
    if not math.isfinite(rate) or rate < 0:
        raise PriceTableConfigError(
            f"price table entry {key!r}.{field} must be a non-negative finite number"
        )
    return rate


def configure_price_table(app) -> None:
    """Validate ``LLM_PRICE_TABLE`` at startup, degrading safely if invalid.

    A valid table is stored in ``app.extensions['price_table']`` and used by all
    pricing lookups. An invalid table logs a clear error, falls back to the
    shipped defaults, and never raises out of ``create_app`` or a request.
    """
    raw = app.config.get(PRICE_TABLE_CONFIG_KEY, "")
    try:
        table = parse_price_table(raw)
        error = None
    except PriceTableConfigError as exc:
        table = _normalized_table(DEFAULT_PRICE_TABLE)
        error = str(exc)
        logger.error(
            "Invalid %s configuration; using default price estimates: %s",
            PRICE_TABLE_CONFIG_KEY,
            error,
        )
    app.extensions["price_table"] = table
    app.config["PRICE_TABLE_ERROR"] = error


def active_price_table() -> dict[tuple[str, str], ModelPrice]:
    """Return the configured table, or the shipped defaults outside an app."""
    if has_app_context():
        table = current_app.extensions.get("price_table")
        if table:
            return table
    return _normalized_table(DEFAULT_PRICE_TABLE)


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalized_table(table=None) -> dict[tuple[str, str], ModelPrice]:
    """Return ``table`` (default: the active/shipped table) with normalized keys."""
    if table is None:
        source = active_price_table() if has_app_context() else DEFAULT_PRICE_TABLE
    else:
        source = table
    return {
        (_normalize(provider), _normalize(model)): price
        for (provider, model), price in source.items()
    }


def _workspace_pk(workspace_id):
    """Return the integer primary key for a workspace id or model instance."""
    if workspace_id is None:
        return None
    return getattr(workspace_id, "id", workspace_id)


def _lookup_keys(provider_key: str, model_key: str) -> tuple[tuple[str, str], ...]:
    """Return the exact-then-wildcard lookup order for a provider/model pair."""
    return (
        (provider_key, model_key),
        (provider_key, WILDCARD),
        (WILDCARD, model_key),
        (WILDCARD, WILDCARD),
    )


def _normalized_overrides(workspace_id) -> dict[tuple[str, str], ModelPrice]:
    """Load a workspace's overrides as normalized :class:`ModelPrice` entries.

    The database is only queried inside an application context. Outside one
    (for example in pure unit tests of the global table) there are simply no
    overrides, so the global table remains usable.
    """
    workspace_pk = _workspace_pk(workspace_id)
    if workspace_pk is None or not has_app_context():
        return {}

    from app.models import WorkspacePriceOverride

    rows = WorkspacePriceOverride.query.filter_by(workspace_id=workspace_pk).all()
    return {
        (_normalize(row.provider), _normalize(row.model)): ModelPrice(
            provider=row.provider,
            model=row.model,
            input_per_1k_usd=float(row.input_per_1k_usd),
            output_per_1k_usd=float(row.output_per_1k_usd),
            estimated=False,
        )
        for row in rows
    }


def workspace_price_table(workspace_id=None, *, table=None) -> dict[tuple[str, str], ModelPrice]:
    """Return the global price table overlaid with a workspace's overrides.

    Every workspace override replaces the same ``(provider, model)`` key in the
    global table. Use :func:`get_model_price` for lookups: it additionally
    guarantees that *any* matching workspace override (including a wildcard)
    wins over *every* global fallback.
    """
    merged = _normalized_table(table)
    merged.update(_normalized_overrides(workspace_id))
    return merged


def get_model_price(
    provider: str | None,
    model: str | None,
    *,
    table=None,
    workspace_id=None,
    workspace=None,
) -> ModelPrice | None:
    """Return the price entry for ``provider``/``model``, or ``None``.

    Matching is case-insensitive and ignores surrounding whitespace. Workspace
    overrides are checked first when ``workspace_id`` (or a ``workspace``
    instance) is supplied, in the same exact-then-wildcard order, so an override
    always takes precedence over a global fallback. The global table is checked
    next using: exact, ``(provider, "*")``, ``("*", model)`` and ``("*", "*")``.
    Unknown combinations return ``None`` (cost unknown) rather than assuming
    zero.
    """
    if workspace_id is None and workspace is not None:
        workspace_id = workspace

    provider_key = _normalize(provider)
    model_key = _normalize(model)
    keys = _lookup_keys(provider_key, model_key)

    if _workspace_pk(workspace_id) is not None:
        overrides = _normalized_overrides(workspace_id)
        for key in keys:
            price = overrides.get(key)
            if price is not None:
                return price

    normalized = _normalized_table(table)
    for key in keys:
        price = normalized.get(key)
        if price is not None:
            return price
    return None


def get_workspace_model_price(
    provider: str | None,
    model: str | None,
    *,
    workspace_id,
    table=None,
) -> ModelPrice | None:
    """Return the effective price for a workspace (override, then global)."""
    return get_model_price(provider, model, table=table, workspace_id=workspace_id)


def merge_price_tables(base=None, overrides=None) -> dict[tuple[str, str], ModelPrice]:
    """Merge two price tables, with ``overrides`` replacing matching keys.

    ``overrides`` is a mapping keyed by ``(provider, model)`` whose values are
    :class:`ModelPrice` entries, so callers can combine a workspace table with
    another source without touching the database.
    """
    merged = _normalized_table(base)
    if overrides:
        merged.update(_normalized_table(overrides))
    return merged


def list_model_prices(
    provider: str | None = None,
    *,
    table=None,
    workspace_id=None,
) -> list[ModelPrice]:
    """Return the price entries, optionally filtered by ``provider``.

    Workspace overrides replace matching global keys. Results are sorted by
    provider then model so the table (and any API built on it) is stable across
    runs.
    """
    wanted = _normalize(provider)
    normalized = _normalized_table(table)
    normalized.update(_normalized_overrides(workspace_id))
    entries = [
        price
        for (entry_provider, _), price in normalized.items()
        if not wanted or entry_provider == wanted
    ]
    return sorted(entries, key=lambda price: (price.provider, price.model))


def price_table(*, table=None, workspace_id=None) -> dict[str, list[dict[str, Any]]]:
    """Return the whole table grouped by provider (JSON-friendly).

    Pass ``workspace_id`` to return the effective table for that workspace.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for price in list_model_prices(table=table, workspace_id=workspace_id):
        grouped.setdefault(price.provider, []).append(price.to_dict())
    return grouped


def estimate_cost_usd(
    provider: str | None,
    model: str | None,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    table=None,
    workspace_id=None,
    workspace=None,
) -> float | None:
    """Estimate USD cost for a completion, or ``None`` when the price is unknown.

    Token counts must be non-negative integers; anything else raises
    :class:`ValueError` so callers cannot silently report a negative cost.
    Each count is clamped to :data:`MAX_TOKEN_COUNT` before the arithmetic so
    an implausible value cannot overflow the estimate. When a workspace is
    supplied, its overrides are used before the global table.
    """
    prompt = int(prompt_tokens)
    completion = int(completion_tokens)
    if prompt < 0 or completion < 0:
        raise ValueError("Token counts must be non-negative.")
    prompt = min(prompt, MAX_TOKEN_COUNT)
    completion = min(completion, MAX_TOKEN_COUNT)

    price = get_model_price(
        provider,
        model,
        table=table,
        workspace_id=workspace_id,
        workspace=workspace,
    )
    if price is None:
        return None
    estimate = prompt / 1000 * price.input_per_1k_usd + completion / 1000 * price.output_per_1k_usd
    return round_cost(estimate)


def estimate_workspace_cost_usd(
    provider: str | None,
    model: str | None,
    *,
    workspace_id,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    table=None,
) -> float | None:
    """Estimate cost using a workspace's overrides, falling back to global."""
    return estimate_cost_usd(
        provider,
        model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        table=table,
        workspace_id=workspace_id,
    )


class PriceOverrideError(ValueError):
    """A workspace price override could not be created or removed."""


def _workspace_for_overrides(workspace=None, workspace_id=None):
    """Resolve a workspace model from either a model instance or an id."""
    from app.extensions import db
    from app.models import Workspace

    target = workspace if workspace is not None else workspace_id
    if target is None:
        raise PriceOverrideError("A workspace is required.")
    if not hasattr(target, "user_id"):
        try:
            target = db.session.get(Workspace, int(target))
        except (TypeError, ValueError) as exc:
            raise PriceOverrideError("A valid workspace is required.") from exc
    if target is None:
        raise PriceOverrideError("Workspace not found.")
    return target


def _assert_owner(workspace, user) -> None:
    """Raise ``PermissionError`` when ``user`` is not the workspace owner."""
    if user is None:
        return
    user_id = getattr(user, "id", user)
    if workspace.user_id != user_id:
        raise PermissionError("Only the workspace owner may manage price overrides.")


def _coerce_rate(value, name: str) -> float:
    """Validate a non-negative, finite per-1k-token rate."""
    try:
        rate = float(value)
    except (TypeError, ValueError) as exc:
        raise PriceOverrideError(f"{name} must be numeric.") from exc
    if not math.isfinite(rate) or rate < 0:
        raise PriceOverrideError(f"{name} must be a non-negative finite number.")
    return rate


def _normalize_override_key(value, name: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise PriceOverrideError(f"{name} is required.")
    if len(text) > limit:
        raise PriceOverrideError(f"{name} must be at most {limit} characters.")
    return text.lower()


def _serialize_override(row) -> dict:
    return row.to_dict() if hasattr(row, "to_dict") else dict(row)


def list_workspace_price_overrides(workspace=None, *, workspace_id=None, user=None) -> list:
    """Return a workspace's overrides, optionally enforcing owner access."""
    from app.models import WorkspacePriceOverride

    resolved = _workspace_for_overrides(workspace, workspace_id)
    _assert_owner(resolved, user)
    return (
        WorkspacePriceOverride.query.filter_by(workspace_id=resolved.id)
        .order_by(WorkspacePriceOverride.provider, WorkspacePriceOverride.model)
        .all()
    )


def set_workspace_price_override(
    workspace=None,
    provider=None,
    model=None,
    input_per_1k_usd=0.0,
    output_per_1k_usd=0.0,
    *,
    workspace_id=None,
    user=None,
    owner_id=None,
    commit=True,
):
    """Create or update one provider/model override for a workspace.

    Only the workspace owner may call this. The rates are USD per 1,000 tokens
    and must be non-negative finite numbers.
    """
    from app.extensions import db
    from app.models import WorkspacePriceOverride

    resolved = _workspace_for_overrides(workspace, workspace_id)
    _assert_owner(resolved, user)
    provider_key = _normalize_override_key(provider, "provider", 50)
    model_key = _normalize_override_key(model, "model", 100)
    input_rate = _coerce_rate(input_per_1k_usd, "input_per_1k_usd")
    output_rate = _coerce_rate(output_per_1k_usd, "output_per_1k_usd")

    row = WorkspacePriceOverride.query.filter_by(
        workspace_id=resolved.id,
        provider=provider_key,
        model=model_key,
    ).first()
    if row is None:
        row = WorkspacePriceOverride(
            workspace_id=resolved.id,
            provider=provider_key,
            model=model_key,
        )
        db.session.add(row)
    row.owner_id = getattr(owner_id, "id", owner_id) or resolved.user_id
    row.input_per_1k_usd = input_rate
    row.output_per_1k_usd = output_rate
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return row


def delete_workspace_price_override(
    workspace=None,
    provider=None,
    model=None,
    *,
    workspace_id=None,
    user=None,
    commit=True,
) -> bool:
    """Delete a workspace override, returning ``True`` when a row was removed."""
    from app.extensions import db
    from app.models import WorkspacePriceOverride

    resolved = _workspace_for_overrides(workspace, workspace_id)
    _assert_owner(resolved, user)
    provider_key = _normalize_override_key(provider, "provider", 50)
    model_key = _normalize_override_key(model, "model", 100)
    row = WorkspacePriceOverride.query.filter_by(
        workspace_id=resolved.id,
        provider=provider_key,
        model=model_key,
    ).first()
    if row is None:
        return False
    db.session.delete(row)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return True


#: Readable aliases for callers that describe the operation differently.
upsert_workspace_price_override = set_workspace_price_override
remove_workspace_price_override = delete_workspace_price_override
get_effective_model_price = get_workspace_model_price
effective_price_table = workspace_price_table
get_price = get_model_price


__all__ = [
    "DEFAULT_PRICE_TABLE",
    "ESTIMATE_DISCLAIMER",
    "MAX_TOKEN_COUNT",
    "PRICE_TABLE_CONFIG_KEY",
    "WILDCARD",
    "ModelPrice",
    "PriceOverrideError",
    "PriceTableConfigError",
    "active_price_table",
    "configure_price_table",
    "delete_workspace_price_override",
    "effective_price_table",
    "estimate_cost_usd",
    "estimate_workspace_cost_usd",
    "get_effective_model_price",
    "get_model_price",
    "get_price",
    "get_workspace_model_price",
    "list_model_prices",
    "list_workspace_price_overrides",
    "merge_price_tables",
    "parse_price_table",
    "price_table",
    "remove_workspace_price_override",
    "set_workspace_price_override",
    "upsert_workspace_price_override",
    "workspace_price_table",
]
