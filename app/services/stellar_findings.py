"""Structured-finding parsing, normalization, and persistence helpers.

Turns the free-text output of the ``stellar_security`` analysis into a bounded
list of structured findings (severity, category, file+line, confidence,
evidence, explanation, remediation). Parsing is intentionally defensive:

* The model is asked to end its answer with a JSON block; we accept the whole
  body when it is JSON, or the first balanced JSON object embedded anywhere in
  the text (e.g. inside ```json ... ``` fences).
* Malformed or missing JSON never crashes the analysis — callers fall back to
  the narrative result with an empty findings list.
* Values are normalized to the shared vocabulary (severity, category,
  confidence), length-bounded, and capped in number. We never fabricate a
  finding from prose.

``persist_findings`` replaces the previous run's findings for a project
(analysis re-runs produce a fresh, current picture) and is safe to call from
within the caller's existing transaction (the analysis route commits).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.extensions import db
from app.models.stellar_security_finding import (
    CONFIDENCES,
    SEVERITIES,
    StellarSecurityFinding,
)

MAX_FINDINGS = 50
_MAX_FILE_CHARS = 2000
_MAX_TEXT_CHARS = 6000

#: Severity synonyms -> canonical severity.
_SEVERITY_ALIASES = {
    "crit": "critical",
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "moderate": "medium",
    "low": "low",
    "info": "informational",
    "informational": "informational",
    "information": "informational",
}

#: Category synonyms -> canonical Stellar category.
_CATEGORY_ALIASES = {
    "access-control": "authorization",
    "access control": "authorization",
    "auth": "authorization",
    "authorization": "authorization",
    "admin": "admin-controls",
    "admin-controls": "admin-controls",
    "panic": "error-handling",
    "unwrap": "error-handling",
    "error-handling": "error-handling",
    "cross-contract": "cross-contract",
    "cross contract": "cross-contract",
    "secrets": "secrets",
    "secret": "secrets",
    "hardcoded-secret": "secrets",
    "credentials": "secrets",
    "storage": "storage",
    "extend-ttl": "storage",
    "testing": "testing",
    "tests": "testing",
    "test-coverage": "testing",
    "configuration": "configuration",
    "config": "configuration",
    "config-hygiene": "configuration",
}


def _as_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value) if value is not None else ""
    return (text or "").strip()[:limit]


def _severity(value: Any) -> str:
    text = _as_text(value, 64).lower().strip()
    canonical = _SEVERITY_ALIASES.get(text)
    if canonical is None and text.startswith("severity"):
        canonical = _SEVERITY_ALIASES.get(text.split(":")[-1].strip())
    return canonical if canonical in SEVERITIES else "medium"


def _category(value: Any) -> str:
    text = _as_text(value, 64).lower().strip()
    text = re.sub(r"[^a-z0-9 -]", "", text).strip()
    return _CATEGORY_ALIASES.get(text, "other")


def _confidence(value: Any) -> str:
    text = _as_text(value, 64).lower()
    if "confirm" in text:
        return "confirmed"
    if text in CONFIDENCES and text != "suggestion":
        return text
    return "suggestion"


def _line(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(1, value) if value > 0 else None
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            number = int(match.group())
            return number if number > 0 else None
    return None


def _clean_finding(item: Any) -> dict[str, Any] | None:
    """Normalize a raw parsed finding; return ``None`` when unusable."""
    if not isinstance(item, dict):
        return None
    file_path = _as_text(item.get("file") or item.get("path"), _MAX_FILE_CHARS) or None
    explanation = _as_text(
        item.get("explanation") or item.get("summary") or item.get("detail"), _MAX_TEXT_CHARS
    )
    if not explanation and not file_path:
        return None
    return {
        "file": file_path,
        "line": _line(item.get("line") or item.get("start_line")),
        "severity": _severity(item.get("severity") or item.get("risk")),
        "category": _category(item.get("category") or item.get("type")),
        "confidence": _confidence(item.get("confidence") or item.get("confirmation")),
        "evidence": _as_text(item.get("evidence") or item.get("snippet"), _MAX_TEXT_CHARS) or None,
        "explanation": explanation,
        "recommendation": _as_text(
            item.get("remediation") or item.get("recommendation") or item.get("fix"),
            _MAX_TEXT_CHARS,
        )
        or None,
    }


def _extract_json_object(text: str) -> Any | None:
    """Return the first balanced JSON value found in ``text`` or ``None``."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except (ValueError, TypeError):
            continue
        return value
    return None


def parse_stellar_findings(text: str | None) -> list[dict[str, Any]]:
    """Parse a bounded, normalized findings list out of analysis output.

    Accepts (1) a whole-body JSON object or array, or (2) a JSON object embedded
    in the text (after any ```json fence). Returns an empty list when nothing
    structured is found — never raises, never fabricates from prose.
    """
    if not text:
        return []
    stripped = text.strip()
    data = None
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except (ValueError, TypeError):
            data = _extract_json_object(stripped)
    else:
        data = _extract_json_object(stripped)

    raw_items: list[Any] = []
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("findings") or data.get("results") or []
        if isinstance(raw_items, dict):  # tolerate {"findings": {...}} single item
            raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return []

    findings: list[dict[str, Any]] = []
    for item in raw_items[:MAX_FINDINGS]:
        cleaned = _clean_finding(item)
        if cleaned is not None:
            findings.append(cleaned)
    return findings


def replace_project_findings(project_id: int, findings: list[dict[str, Any]]) -> int:
    """Replace ``project_id``'s stored findings with ``findings`` (caller commits).

    Returns the number of persisted findings. Runs in the caller's transaction
    so a single analysis request persists atomically with its own activity row.
    """
    StellarSecurityFinding.query.filter_by(project_id=project_id).delete()
    for finding in findings:
        db.session.add(
            StellarSecurityFinding(
                project_id=project_id,
                file=finding.get("file"),
                line=finding.get("line"),
                severity=finding.get("severity") or "medium",
                category=finding.get("category") or "other",
                confidence=finding.get("confidence") or "suggestion",
                evidence=finding.get("evidence"),
                explanation=finding.get("explanation") or "",
                recommendation=finding.get("recommendation"),
            )
        )
    return len(findings)


def list_project_findings(project_id: int) -> list[dict[str, Any]]:
    """Return ``project_id``'s stored findings (newest first)."""
    rows = (
        StellarSecurityFinding.query.filter_by(project_id=project_id)
        .order_by(StellarSecurityFinding.id.desc())
        .limit(MAX_FINDINGS)
        .all()
    )
    return [row.to_dict() for row in rows]
