"""Versioned webhook event catalog (#666).

The catalog is the single source of truth for the event names, the fields each
payload must contain, and whether an event is safe to retry. Producers and
consumers validate against it instead of guessing at string shapes.

Versioning rules:

- ``CATALOG_VERSION`` changes when a payload gains or removes a required field
  in a way consumers must notice.
- Event names are stable; a renamed event is added as a new name and the old one
  is marked deprecated.
- Adding an optional field does not require a version bump.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Version of the catalog itself.
CATALOG_VERSION = 1

#: Event names grouped by area. Every name here is validated by the helpers.
EVENT_AREAS: dict[str, tuple[str, ...]] = {
    "project": ("project.created", "project.analysis.completed"),
    "review": ("review.completed", "review.failed"),
    "usage": ("usage.recorded",),
    "member": ("member.invited", "member.removed"),
    "security": ("security.finding", "security.finding.resolved"),
}


class CatalogError(ValueError):
    """Raised when an event name or payload is invalid."""

    def __init__(self, message: str, *, code: str = "invalid_event") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EventDefinition:
    """One versioned event in the catalog."""

    name: str
    area: str
    version: int
    retry_safe: bool
    required_fields: tuple[str, ...]
    description: str


def _definition(
    name: str,
    area: str,
    *,
    retry_safe: bool,
    required_fields: Iterable[str],
    description: str,
) -> EventDefinition:
    return EventDefinition(
        name=name,
        area=area,
        version=CATALOG_VERSION,
        retry_safe=retry_safe,
        required_fields=tuple(required_fields),
        description=description,
    )


#: The full catalog, keyed by event name.
EVENTS: dict[str, EventDefinition] = {
    definition.name: definition
    for definition in (
        _definition(
            "project.created",
            "project",
            retry_safe=True,
            required_fields=("project_id", "workspace_id"),
            description="A project was imported into a workspace.",
        ),
        _definition(
            "project.analysis.completed",
            "project",
            retry_safe=True,
            required_fields=("project_id", "status"),
            description="Analysis of a project finished.",
        ),
        _definition(
            "review.completed",
            "review",
            retry_safe=True,
            required_fields=("review_id", "project_id", "status"),
            description="A review completed successfully.",
        ),
        _definition(
            "review.failed",
            "review",
            retry_safe=True,
            required_fields=("review_id", "project_id", "error"),
            description="A review failed; retrying the delivery is safe.",
        ),
        _definition(
            "usage.recorded",
            "usage",
            retry_safe=False,
            required_fields=("workspace_id", "total_tokens"),
            description="A usage ledger entry was recorded; not safe to repeat as an event.",
        ),
        _definition(
            "member.invited",
            "member",
            retry_safe=True,
            required_fields=("workspace_id", "invitation_id"),
            description="A workspace member was invited.",
        ),
        _definition(
            "member.removed",
            "member",
            retry_safe=True,
            required_fields=("workspace_id", "user_id"),
            description="A workspace member was removed.",
        ),
        _definition(
            "security.finding",
            "security",
            retry_safe=True,
            required_fields=("project_id", "severity", "title"),
            description="A security finding was recorded for a project.",
        ),
        _definition(
            "security.finding.resolved",
            "security",
            retry_safe=True,
            required_fields=("finding_id", "project_id"),
            description="A security finding was resolved.",
        ),
    )
}


def event_names() -> list[str]:
    """Return every catalog event name, sorted."""
    return sorted(EVENTS)


def event_names_for_area(area: str) -> list[str]:
    """Return the event names in ``area``, sorted."""
    key = (area or "").strip().lower()
    if key not in EVENT_AREAS:
        raise CatalogError(f"unknown event area: {area!r}", code="unknown_area")
    return sorted(EVENT_AREAS[key])


def get_event(name: str) -> EventDefinition:
    """Return the definition for ``name`` or raise :class:`CatalogError`."""
    key = (name or "").strip().lower()
    definition = EVENTS.get(key)
    if definition is None:
        raise CatalogError(f"unknown event: {name!r}", code="unknown_event")
    return definition


def is_retry_safe(name: str) -> bool:
    """Whether delivery of ``name`` may be retried without duplicating a side effect."""
    return get_event(name).retry_safe


def validate_payload(name: str, payload: Mapping) -> dict:
    """Validate a payload for ``name`` and return a shallow copy.

    Raises :class:`CatalogError` for an unknown event, a non-mapping payload, or
    a missing required field. Extra fields are preserved for forward
    compatibility.
    """
    definition = get_event(name)
    if not isinstance(payload, Mapping):
        raise CatalogError("event payload must be a mapping", code="invalid_payload")
    missing = [field for field in definition.required_fields if field not in payload]
    if missing:
        raise CatalogError(
            "event payload is missing required field(s): " + ", ".join(missing),
            code="missing_field",
        )
    return dict(payload)


__all__ = [
    "CATALOG_VERSION",
    "EVENTS",
    "EVENT_AREAS",
    "CatalogError",
    "EventDefinition",
    "event_names",
    "event_names_for_area",
    "get_event",
    "is_retry_safe",
    "validate_payload",
]
