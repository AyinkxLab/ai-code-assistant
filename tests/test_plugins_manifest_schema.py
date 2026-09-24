"""Consistency tests for the plugin manifest contract (#165).

The manifest is described by two layers: the declared JSON Schema at
``plugins/plugin.schema.json`` and the runtime validator in
``PluginManifest.from_dict``. These tests drive a shared valid/invalid corpus
through *both* layers and assert they agree, so the schema and the code cannot
silently drift apart.

The schema checker below is a small, dependency-free interpreter for the subset
of draft-07 keywords the manifest schema uses (type, required, properties,
additionalProperties, pattern, enum, const, minLength/maxLength, items,
minItems, uniqueItems). It reads the rules from the schema file itself, so the
test reflects the published contract rather than a hard-coded copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.services.plugins import ManifestValidationError, PluginManifest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "plugins" / "plugin.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Minimal JSON Schema (draft-07 subset) checker
# ---------------------------------------------------------------------------


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _schema_errors(value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Return the schema violations for ``value`` (empty when it satisfies it)."""
    errors: list[str] = []
    label = path or "<root>"

    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(value, item) for item in allowed):
            return [f"{label}: expected type {expected_type}"]

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{label}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{label}: expected const {schema['const']!r}")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{label}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{label}: longer than maxLength {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{label}: does not match pattern {schema['pattern']!r}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{label}: fewer than minItems {schema['minItems']}")
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True, default=str) for item in value]
            if len(set(serialized)) != len(serialized):
                errors.append(f"{label}: items are not unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, item_schema, f"{label}[{index}]"))

    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{label}: missing required property {required!r}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in properties:
                errors.extend(_schema_errors(item, properties[key], child_path))
            elif additional is False:
                errors.append(f"{child_path}: additional property not allowed")
            elif isinstance(additional, dict):
                errors.extend(_schema_errors(item, additional, child_path))

    return errors


def _schema_accepts(data: Any) -> bool:
    return not _schema_errors(data, SCHEMA)


def _runtime_accepts(data: Any) -> bool:
    try:
        PluginManifest.from_dict(data)
    except ManifestValidationError:
        return False
    return True


# ---------------------------------------------------------------------------
# Shared valid/invalid corpus
# ---------------------------------------------------------------------------

VALID: dict[str, Any] = {
    "id": "test-plugin",
    "name": "Test Plugin",
    "version": "0.1.0",
    "description": "A test plugin",
    "author": "Test Author",
    "entry_point": "plugins.test:TestPlugin",
    "capabilities": ["PROJECT_READ"],
}

ALL_CAPABILITIES = [
    "PROJECT_READ",
    "PROJECT_WRITE",
    "PROJECT_DELETE",
    "WORKSPACE_READ",
    "WORKSPACE_WRITE",
    "GITHUB_READ",
    "GITHUB_WRITE",
    "AI_ACCESS",
    "AI_ANALYSIS",
    "NOTIFICATION_CREATE",
    "STELLAR_READ",
    "STELLAR_WRITE",
    "STELLAR_ANALYSIS",
    "REVIEW_READ",
    "REVIEW_CREATE",
]


def _case(case_id: str, expected_valid: bool, **overrides: Any) -> tuple:
    return (case_id, {**VALID, **overrides}, expected_valid)


def _build_cases() -> list[tuple]:
    cases: list[tuple] = [
        ("minimal_valid", dict(VALID), True),
        (
            "full_valid",
            {
                **VALID,
                "compatibility": ">=0.8.0",
                "permissions": ["read:project:files"],
                "dependencies": ["stellar-sdk>=11.0.0"],
                "configuration": {"enabled_networks": ["testnet"]},
            },
            True,
        ),
        ("all_capabilities_valid", {**VALID, "capabilities": list(ALL_CAPABILITIES)}, True),
        ("id_max_length_valid", {**VALID, "id": "a" * 64}, True),
        ("valid_version_prerelease", {**VALID, "version": "1.0.0-alpha+build"}, True),
        (
            "valid_entry_point_nested",
            {**VALID, "entry_point": "nested.modules.plugin:PluginClass"},
            True,
        ),
        # Required fields
        ("missing_version", {k: v for k, v in VALID.items() if k != "version"}, False),
        ("missing_capabilities", {k: v for k, v in VALID.items() if k != "capabilities"}, False),
        # id
        _case("id_uppercase", False, id="TestPlugin"),
        _case("id_leading_digit", False, id="123plugin"),
        _case("id_space", False, id="test plugin"),
        _case("id_special_char", False, id="test@plugin"),
        _case("id_too_long", False, id="a" * 65),
        _case("id_non_string", False, id=123),
    ]

    # version (invalid)
    for index, value in enumerate(["1", "1.0", "v1.0.0", "01.02.03", "1.0.0.0"]):
        cases.append((f"version_invalid_{index}", {**VALID, "version": value}, False))
    cases.append(("version_non_string", {**VALID, "version": 1}, False))

    # entry_point (invalid)
    for index, value in enumerate(["module", "module:", ":ClassName", "module.path@ClassName"]):
        cases.append((f"entry_point_invalid_{index}", {**VALID, "entry_point": value}, False))

    # capabilities
    cases.append(("capability_unknown", {**VALID, "capabilities": ["UNKNOWN_CAPABILITY"]}, False))
    cases.append(("capabilities_empty", {**VALID, "capabilities": []}, False))
    cases.append(
        ("capability_duplicate", {**VALID, "capabilities": ["PROJECT_READ", "PROJECT_READ"]}, False)
    )
    cases.append(("capabilities_not_list", {**VALID, "capabilities": "PROJECT_READ"}, False))
    cases.append(("capability_non_string", {**VALID, "capabilities": [123]}, False))

    # permissions / dependencies
    cases.append(("permissions_not_list", {**VALID, "permissions": "read:project"}, False))
    cases.append(("permissions_non_string_item", {**VALID, "permissions": [1]}, False))
    cases.append(
        ("permissions_duplicate", {**VALID, "permissions": ["read:project", "read:project"]}, False)
    )
    cases.append(("dependencies_not_list", {**VALID, "dependencies": "requests"}, False))
    cases.append(("dependencies_non_string_item", {**VALID, "dependencies": [1]}, False))
    cases.append(
        ("dependencies_duplicate", {**VALID, "dependencies": ["requests", "requests"]}, False)
    )

    # configuration
    cases.append(("configuration_not_object", {**VALID, "configuration": ["x"]}, False))
    cases.append(("configuration_string", {**VALID, "configuration": "x"}, False))

    # additionalProperties
    cases.append(("unknown_field", {**VALID, "unexpected": True}, False))

    # string bounds
    cases.append(("name_empty", {**VALID, "name": ""}, False))
    cases.append(("name_too_long", {**VALID, "name": "x" * 257}, False))
    cases.append(("description_too_long", {**VALID, "description": "x" * 2049}, False))
    cases.append(("author_too_long", {**VALID, "author": "x" * 257}, False))

    return cases


CASES = _build_cases()
CASE_IDS = [case[0] for case in CASES]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case_id", "data", "expected_valid"), CASES, ids=CASE_IDS)
def test_schema_and_runtime_agree(case_id, data, expected_valid):
    """Every corpus case is judged identically by the schema and the validator."""
    schema_valid = _schema_accepts(data)
    runtime_valid = _runtime_accepts(data)

    assert (
        schema_valid is runtime_valid
    ), f"{case_id}: schema_valid={schema_valid} but runtime_valid={runtime_valid}"
    assert schema_valid is expected_valid, f"{case_id}: schema did not match the expectation"


def test_non_object_manifest_rejected_by_both():
    assert not _schema_accepts([])
    with pytest.raises(ManifestValidationError):
        PluginManifest.from_dict([])  # type: ignore[arg-type]


def test_to_dict_round_trips_through_runtime_validator():
    """A runtime-serialized manifest (including trust metadata) re-validates."""
    manifest = PluginManifest.from_dict(dict(VALID))
    reparsed = PluginManifest.from_dict(manifest.to_dict())
    assert reparsed.id == manifest.id
    assert reparsed.compatibility is None


def test_published_example_manifests_satisfy_both_layers():
    manifests = [
        REPO_ROOT / "examples" / "hello-plugin" / "manifest.json",
        REPO_ROOT / "examples" / "plugins" / "hello_world" / "manifest.json",
    ]
    for manifest_path in manifests:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert _schema_accepts(data), f"{manifest_path} violates the schema"
        assert _runtime_accepts(data), f"{manifest_path} violates the runtime validator"
