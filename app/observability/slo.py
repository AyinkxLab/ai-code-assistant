"""SLO, dashboard and alert definitions for the emitted metrics (#704).

The definitions live in ``slo_definitions.json`` next to this module. This
module only loads and validates them; it does not evaluate PromQL. Validation
is intentionally strict about metric names: every expression must reference a
metric family that the in-process registry actually emits, so a dashboard can
never silently point at a metric the application does not publish.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFINITIONS_PATH = Path(__file__).with_name("slo_definitions.json")

#: PromQL functions/keywords that are not metric names.
PROMQL_TOKENS = frozenset(
    {
        "sum",
        "rate",
        "by",
        "without",
        "histogram_quantile",
        "avg",
        "max",
        "min",
        "count",
        "increase",
        "irate",
        "le",
        "on",
        "ignoring",
        "group_left",
        "group_right",
    }
)

#: Label names used by the definitions. They are not metrics.
LABEL_NAMES = frozenset(
    {
        "method",
        "endpoint",
        "status",
        "provider",
        "model",
        "direction",
        "severity",
        "slo",
    }
)

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DERIVED_SUFFIXES = ("_bucket", "_sum", "_count")


@lru_cache(maxsize=1)
def load_definitions() -> dict[str, Any]:
    """Return the parsed definitions document (read once per process)."""
    with DEFINITIONS_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _base_metric(token: str, emitted: set[str]) -> str | None:
    """Return the metric family a token belongs to, or ``None``.

    Histogram samples carry ``_bucket``/``_sum``/``_count`` suffixes; they are
    folded back to the family name the registry publishes.
    """
    if token in emitted:
        return token
    for suffix in _DERIVED_SUFFIXES:
        if token.endswith(suffix):
            base = token[: -len(suffix)]
            if base in emitted:
                return base
    return None


def referenced_metrics(expr: str, emitted: set[str]) -> set[str]:
    """Return the metric families referenced by a PromQL expression."""
    found = set()
    for token in _TOKEN.findall(expr or ""):
        if token in PROMQL_TOKENS or token in LABEL_NAMES:
            continue
        base = _base_metric(token, emitted)
        if base is not None:
            found.add(base)
        elif "_" in token:
            # An underscore-bearing token that is not a known function, label
            # or emitted metric is a likely typo. Keep it so validation fails
            # with a useful name instead of silently ignoring it.
            found.add(token)
    return found


def _expressions(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Return ``(location, expression)`` for every expression in the document."""
    found: list[tuple[str, str]] = []
    for index, objective in enumerate(document.get("objectives", [])):
        found.append((f"objectives[{index}].expr", objective.get("expr", "")))
    for panel in document.get("dashboard", {}).get("panels", []):
        for target in panel.get("targets", []):
            found.append((f"panel {panel.get('id')}/{target.get('refId')}", target.get("expr", "")))
    for index, alert in enumerate(document.get("alerts", [])):
        found.append((f"alerts[{index}].expr", alert.get("expr", "")))
    return found


def validate_definitions(
    document: dict[str, Any], emitted_names: tuple[str, ...] | list[str]
) -> list[str]:
    """Return human-readable validation errors (empty when everything is valid)."""
    errors: list[str] = []
    emitted = set(emitted_names)
    if not emitted:
        return ["the metric registry emitted no metric names"]

    contract = document.get("metrics_contract") or {}
    if not isinstance(contract, dict):
        return ["metrics_contract must be an object"]
    for name in contract:
        if name not in emitted:
            errors.append(f"metrics_contract references unknown metric {name!r}")

    objectives = document.get("objectives") or []
    if not objectives:
        errors.append("at least one SLO objective is required")
    for index, objective in enumerate(objectives):
        if not objective.get("expr"):
            errors.append(f"objectives[{index}] has no expr")
        target = objective.get("target", objective.get("target_ms"))
        if target is None or not isinstance(target, (int, float)) or target <= 0:
            errors.append(f"objectives[{index}] has no positive target")

    for location, expr in _expressions(document):
        if not expr:
            errors.append(f"{location} is empty")
            continue
        metrics = referenced_metrics(expr, emitted)
        if not metrics:
            errors.append(f"{location} references no known metric")
        for name in sorted(metrics - emitted):
            errors.append(f"{location} references unknown metric {name!r}")

    alerts = document.get("alerts") or []
    if not alerts:
        errors.append("at least one alert rule is required")
    names = [alert.get("alert") for alert in alerts]
    if len(names) != len(set(names)):
        errors.append("alert names must be unique")
    for index, alert in enumerate(alerts):
        if not alert.get("alert"):
            errors.append(f"alerts[{index}] has no alert name")
        if not alert.get("for"):
            errors.append(f"alerts[{index}] has no 'for' duration")
        if (alert.get("labels") or {}).get("severity") not in {"warning", "critical"}:
            errors.append(f"alerts[{index}] needs a warning or critical severity")
        if not (alert.get("annotations") or {}).get("summary"):
            errors.append(f"alerts[{index}] needs an annotation summary")

    return errors


__all__ = [
    "DEFINITIONS_PATH",
    "load_definitions",
    "referenced_metrics",
    "validate_definitions",
]
