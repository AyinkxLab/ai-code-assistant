"""Filtering, sorting, and unknown-parameter rejection for public lists (#648).

Public list resources accept a small, explicit query grammar:

* filters are plain query parameters (``?status=ready``) and must be on the
  resource's whitelist;
* ``sort`` names one whitelisted field, and ``order`` is ``asc`` or ``desc``;
* pagination parameters ``page``, ``limit`` and ``cursor`` are handled by
  :mod:`app.services.pagination`;
* anything else is rejected instead of silently ignored, so a typo cannot
  produce a misleadingly "unfiltered" result.

Every resource documents its whitelist and default ordering next to the route.
Callers translate :class:`FilterError` into the shared ``invalid_input`` error
shape via :func:`app.api.error_catalog.error_response`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PAGINATION_PARAMS = frozenset({"page", "limit", "cursor"})
SORT_PARAMS = frozenset({"sort", "order"})
ORDERS = ("asc", "desc")


class FilterError(ValueError):
    """Raised for an unknown parameter, unknown field, or malformed value."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass(frozen=True)
class FilterField:
    """One whitelisted filter: its public name, model column, and value kind."""

    column: str
    kind: str = "string"


@dataclass(frozen=True)
class ListQuery:
    """A validated filter/sort request."""

    filters: dict[str, Any] = field(default_factory=dict)
    sort: str = "id"
    column: str = "id"
    descending: bool = True


def _coerce(name: str, raw: str, kind: str) -> Any:
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise FilterError(f"{name} must be an integer.") from None
    if kind == "bool":
        lowered = str(raw).strip().lower()
        if lowered in ("1", "true", "yes"):
            return True
        if lowered in ("0", "false", "no"):
            return False
        raise FilterError(f"{name} must be true or false.")
    return raw


def reject_unknown_parameters(args: Mapping[str, Any], allowed: set[str] | frozenset[str]) -> None:
    """Raise :class:`FilterError` when ``args`` contains an unexpected name."""
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise FilterError(
            "Unknown query parameter(s): "
            + ", ".join(unknown)
            + ". Allowed: "
            + ", ".join(sorted(allowed)),
            details={"unknown": unknown, "allowed": sorted(allowed)},
        )


def parse_list_query(
    args: Mapping[str, Any],
    *,
    filters: Mapping[str, FilterField],
    sorts: Mapping[str, str],
    default_sort: str = "id",
) -> ListQuery:
    """Validate ``args`` against the resource whitelist.

    ``sorts`` maps a public sort name to the model column name. The default
    ordering is the default sort field in descending order.
    """
    if default_sort not in sorts:
        raise ValueError(f"default_sort {default_sort!r} is not a whitelisted sort")

    allowed = set(filters) | set(sorts) | PAGINATION_PARAMS | SORT_PARAMS
    reject_unknown_parameters(args, allowed)

    sort = (args.get("sort") or default_sort).strip()
    if sort not in sorts:
        raise FilterError(
            f"Unknown sort field: {sort}. Allowed: " + ", ".join(sorted(sorts)),
            details={"field": sort, "allowed": sorted(sorts)},
        )

    order = (args.get("order") or "desc").strip().lower()
    if order not in ORDERS:
        raise FilterError(
            "order must be 'asc' or 'desc'.",
            details={"field": "order", "allowed": list(ORDERS)},
        )

    parsed: dict[str, Any] = {}
    for name, spec in filters.items():
        raw = args.get(name)
        if raw is None or raw == "":
            continue
        parsed[spec.column] = _coerce(name, raw, spec.kind)

    return ListQuery(filters=parsed, sort=sort, column=sorts[sort], descending=order == "desc")


__all__ = [
    "PAGINATION_PARAMS",
    "FilterError",
    "FilterField",
    "ListQuery",
    "parse_list_query",
    "reject_unknown_parameters",
]
