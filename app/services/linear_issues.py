"""Create Linear issues from review findings (#681).

The transport is injected as a small protocol so the mapping and idempotency
logic are testable without network access. A finding maps to a deterministic
idempotency key derived from the project and finding id: retrying the sync
returns the existing issue instead of creating a duplicate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

#: Linear priority values (0 = no priority, 1 = urgent, 4 = low).
SEVERITY_PRIORITY: dict[str, int] = {
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
    "info": 4,
}


class LinearError(Exception):
    """A structured Linear integration error."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "linear_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "status_code": self.status_code,
        }


class LinearClient(Protocol):
    """The small transport surface the service needs."""

    def find_issue(self, idempotency_key: str) -> dict | None:
        """Return an existing issue for the key, or ``None``."""

    def create_issue(
        self,
        *,
        title: str,
        description: str,
        priority: int,
        idempotency_key: str,
    ) -> dict:
        """Create an issue and return its ``identifier`` and ``url``."""


@dataclass(frozen=True)
class LinearIssue:
    """The outcome of a finding-to-issue sync."""

    identifier: str
    url: str
    created: bool


def priority_for_severity(severity: str) -> int:
    """Map a review severity to a Linear priority."""
    key = (severity or "").strip().lower()
    priority = SEVERITY_PRIORITY.get(key)
    if priority is None:
        raise LinearError(
            f"unknown finding severity: {severity!r}",
            code="invalid_severity",
        )
    return priority


def idempotency_key(project_id: int, finding_id: str) -> str:
    """Return a stable key for a project/finding pair."""
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise LinearError("finding_id must be a non-empty string", code="invalid_finding")
    if isinstance(project_id, bool) or not isinstance(project_id, int) or project_id < 0:
        raise LinearError("project_id must be a non-negative integer", code="invalid_project")
    digest = hashlib.sha256(f"linear:{project_id}:{finding_id}".encode()).hexdigest()
    return digest[:40]


def _build_description(
    *,
    finding_id: str,
    description: str | None,
    backlink: str | None,
    file: str | None,
    line: int | None,
) -> str:
    parts = [f"Review finding `{finding_id}`."]
    if description and description.strip():
        parts.append(description.strip())
    location = []
    if file:
        location.append(f"`{file}:{line}`" if line is not None else f"`{file}`")
    if location:
        parts.append("Location: " + ", ".join(location))
    if backlink:
        parts.append(f"[View the finding]({backlink})")
    return "\n\n".join(parts)


def create_issue_from_finding(
    *,
    client: LinearClient,
    project_id: int,
    finding_id: str,
    title: str,
    severity: str,
    description: str | None = None,
    backlink: str | None = None,
    file: str | None = None,
    line: int | None = None,
) -> LinearIssue:
    """Create (or return) the Linear issue for a review finding.

    Raises :class:`LinearError` for invalid input, an invalid severity, a
    malformed client response, or a transport failure.
    """
    if not isinstance(title, str) or not title.strip():
        raise LinearError("finding title must be a non-empty string", code="invalid_title")
    priority = priority_for_severity(severity)
    key = idempotency_key(project_id, finding_id)
    body = _build_description(
        finding_id=finding_id,
        description=description,
        backlink=backlink,
        file=file,
        line=line,
    )

    try:
        existing = client.find_issue(key)
        if existing:
            identifier = str(existing.get("identifier") or "").strip()
            url = str(existing.get("url") or "").strip()
            if not identifier or not url:
                raise LinearError(
                    "Linear returned an incomplete existing issue",
                    code="invalid_response",
                )
            return LinearIssue(identifier=identifier, url=url, created=False)

        created = client.create_issue(
            title=title.strip(),
            description=body,
            priority=priority,
            idempotency_key=key,
        )
    except LinearError:
        raise
    except Exception as exc:  # pragma: no cover - defensive transport wrapper
        raise LinearError(f"Linear request failed: {exc}", code="unavailable") from exc

    identifier = str(created.get("identifier") or "").strip()
    url = str(created.get("url") or "").strip()
    if not identifier or not url:
        raise LinearError(
            "Linear returned an incomplete issue",
            code="invalid_response",
        )
    return LinearIssue(identifier=identifier, url=url, created=True)


__all__ = [
    "SEVERITY_PRIORITY",
    "LinearClient",
    "LinearError",
    "LinearIssue",
    "create_issue_from_finding",
    "idempotency_key",
    "priority_for_severity",
]
