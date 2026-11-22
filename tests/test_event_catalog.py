"""Tests for the versioned webhook event catalog (#666)."""

from __future__ import annotations

import pytest

from app.services import event_catalog


def test_catalog_covers_every_area():
    for area, names in event_catalog.EVENT_AREAS.items():
        assert event_catalog.event_names_for_area(area) == sorted(names)
        for name in names:
            definition = event_catalog.get_event(name)
            assert definition.area == area
            assert definition.version == event_catalog.CATALOG_VERSION


def test_event_names_are_sorted_and_unique():
    names = event_catalog.event_names()
    assert names == sorted(names)
    assert len(names) == len(set(names))
    assert set(names) == set(event_catalog.EVENTS)


def test_unknown_events_and_areas_raise_structured_errors():
    with pytest.raises(event_catalog.CatalogError) as excinfo:
        event_catalog.get_event("nope")
    assert excinfo.value.code == "unknown_event"

    with pytest.raises(event_catalog.CatalogError) as excinfo:
        event_catalog.event_names_for_area("billing")
    assert excinfo.value.code == "unknown_area"


def test_validate_payload_requires_fields_and_allows_extra():
    payload = event_catalog.validate_payload(
        "review.completed",
        {"review_id": 1, "project_id": 2, "status": "completed", "extra": True},
    )
    assert payload["extra"] is True

    with pytest.raises(event_catalog.CatalogError) as excinfo:
        event_catalog.validate_payload("review.completed", {"review_id": 1})
    assert excinfo.value.code == "missing_field"
    assert "project_id" in str(excinfo.value)

    with pytest.raises(event_catalog.CatalogError):
        event_catalog.validate_payload("review.completed", ["not", "a", "mapping"])


def test_retry_safety_is_explicit():
    assert event_catalog.is_retry_safe("review.completed")
    assert not event_catalog.is_retry_safe("usage.recorded")
    with pytest.raises(event_catalog.CatalogError):
        event_catalog.is_retry_safe("unknown.event")
