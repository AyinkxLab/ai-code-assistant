"""Typed models for the AI Code Assistant Python SDK.

The models are intentionally permissive about server-added fields (unknown keys
are ignored) but strict about the fields the SDK documents: a malformed
payload raises :class:`~ai_code_assistant.errors.ValidationError` instead of
silently producing a half-valid object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError

__all__ = ["Finding", "Project", "Review", "Usage"]


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{label} must be a JSON object")
    return value


def _required_str(data: Mapping[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}.{key} must be a non-empty string")
    return value


def _required_int(data: Mapping[str, Any], key: str, label: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label}.{key} must be an integer")
    return value


def _optional_int(data: Mapping[str, Any], key: str, label: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{label}.{key} must be an integer or null")
    return value


def _optional_number(data: Mapping[str, Any], key: str, label: str) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}.{key} must be a number or null")
    return float(value)


def _optional_str(data: Mapping[str, Any], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label}.{key} must be a string or null")
    return value


@dataclass(frozen=True)
class Finding:
    """A single review finding."""

    severity: str
    title: str
    id: int | None = None
    category: str | None = None
    description: str | None = None
    path: str | None = None
    line: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> Finding:
        data = _mapping(value, "finding")
        return cls(
            id=_optional_int(data, "id", "finding"),
            severity=_required_str(data, "severity", "finding"),
            category=_optional_str(data, "category", "finding"),
            title=_required_str(data, "title", "finding"),
            description=_optional_str(data, "description", "finding"),
            path=_optional_str(data, "path", "finding"),
            line=_optional_int(data, "line", "finding"),
        )


@dataclass(frozen=True)
class Project:
    """A project imported into a workspace."""

    id: int
    name: str
    owner_id: int | None = None
    description: str | None = None
    github_repo: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> Project:
        data = _mapping(value, "project")
        return cls(
            id=_required_int(data, "id", "project"),
            name=_required_str(data, "name", "project"),
            owner_id=_optional_int(data, "owner_id", "project"),
            description=_optional_str(data, "description", "project"),
            github_repo=_optional_str(data, "github_repo", "project"),
            created_at=_optional_str(data, "created_at", "project"),
            updated_at=_optional_str(data, "updated_at", "project"),
        )


@dataclass(frozen=True)
class Review:
    """A code review with its structured scores and findings."""

    id: int
    status: str
    project_id: int | None = None
    quality_score: float | None = None
    security_score: float | None = None
    tests_score: float | None = None
    findings: list[Finding] = field(default_factory=list)
    created_at: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> Review:
        data = _mapping(value, "review")
        raw_findings = data.get("findings", [])
        if not isinstance(raw_findings, list):
            raise ValidationError("review.findings must be a list")
        return cls(
            id=_required_int(data, "id", "review"),
            status=_required_str(data, "status", "review"),
            project_id=_optional_int(data, "project_id", "review"),
            quality_score=_optional_number(data, "quality_score", "review"),
            security_score=_optional_number(data, "security_score", "review"),
            tests_score=_optional_number(data, "tests_score", "review"),
            findings=[Finding.from_dict(item) for item in raw_findings],
            created_at=_optional_str(data, "created_at", "review"),
        )


@dataclass(frozen=True)
class Usage:
    """Aggregated token/cost usage for a project or workspace."""

    input_tokens: int
    output_tokens: int
    total_tokens: int | None = None
    cost_usd: float | None = None
    period_start: str | None = None
    period_end: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> Usage:
        data = _mapping(value, "usage")
        input_tokens = _required_int(data, "input_tokens", "usage")
        output_tokens = _required_int(data, "output_tokens", "usage")
        total_tokens = _optional_int(data, "total_tokens", "usage")
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens if total_tokens is None else total_tokens,
            cost_usd=_optional_number(data, "cost_usd", "usage"),
            period_start=_optional_str(data, "period_start", "usage"),
            period_end=_optional_str(data, "period_end", "usage"),
        )
