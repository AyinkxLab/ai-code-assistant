"""Focused compatibility matrix for the PEP 440 plugin mechanism (#197).

Contract under test
-------------------
``app.services.plugin_compat.plugin_compatible(compatibility, version)`` answers
"does the plugin's PEP 440 ``compatibility`` range accept this version?" and is
the function the manifest validation / install path (#168) relies on. The matrix
below pins its deterministic behavior:

* A range accepts any version it contains, across ``==``, ``!=``, ``>=``,
  ``<=``, ``>``, ``<``, ``~=``, ``===``, wildcards, and space/comma-separated
  clauses.
* Boundary versions are inclusive/exclusive exactly as PEP 440 defines them
  (e.g. ``>=0.8.0`` accepts ``0.8.0`` but not ``0.7.9``).
* An empty or absent range matches every version.
* A malformed specifier never matches and is reported invalid (fail closed) and
  must never raise.

This module exercises the mechanism only; it changes no application behavior.
"""

import pytest

from app.services.plugin_compat import (
    InvalidCompatibilityError,
    compatibility_status,
    is_valid_compatibility,
    parse_compatibility,
    plugin_compatible,
)

MATRIX = [
    # Exact equality and exclusion.
    ("==1.2.3", "1.2.3", True),
    ("==1.2.3", "1.2.4", False),
    ("!=1.2.3", "1.2.4", True),
    ("!=1.2.3", "1.2.3", False),
    # Inclusive lower boundary (the #197 example).
    (">=0.8.0", "0.8.0", True),
    (">=0.8.0", "0.7.9", False),
    (">=0.8.0", "0.8.1", True),
    # Strict lower boundary.
    (">0.8.0", "0.8.0", False),
    (">0.8.0", "0.8.0.1", True),
    # Inclusive upper boundary.
    ("<=2.0.0", "2.0.0", True),
    ("<=2.0.0", "2.0.1", False),
    # Strict upper boundary.
    ("<2.0.0", "2.0.0", False),
    ("<2.0.0", "1.9.9", True),
    # Compatible-release operator (~=).
    ("~=1.4.2", "1.4.9", True),
    ("~=1.4.2", "1.5.0", False),
    ("~=1.4", "1.9.0", True),
    ("~=1.4", "2.0.0", False),
    # Arbitrary equality and wildcards.
    ("===1.2.3", "1.2.3", True),
    ("==1.4.*", "1.4.7", True),
    ("==1.4.*", "1.5.0", False),
    # Combined clauses (comma- and space-separated).
    (">=1.0.0,<2.0.0", "1.5.0", True),
    (">=1.0.0,<2.0.0", "2.0.0", False),
    (">=1.0.0, <2.0.0", "1.5.0", True),
    (">=1.0.0, !=1.5.0", "1.5.0", False),
    (">=1.0.0, !=1.5.0", "1.6.0", True),
    # Unrestricted ranges match everything.
    ("", "9.9.9", True),
    (None, "9.9.9", True),
    # Prereleases may satisfy a range.
    (">=0.8.0rc1", "0.8.0rc2", True),
]

INVALID_MATRIX = [">=", ">>=1.0.0", "1.2.3.4.5", "==two", "~=x", "not a specifier", "bogus"]


@pytest.mark.parametrize(
    ("compatibility", "version", "expected"),
    MATRIX,
    ids=[f"{spec!r} vs {version!r}" for spec, version, _ in MATRIX],
)
def test_compatibility_matrix(compatibility, version, expected):
    assert plugin_compatible(compatibility, version) is expected


@pytest.mark.parametrize("specifier", INVALID_MATRIX, ids=INVALID_MATRIX)
def test_malformed_specifiers_fail_closed_without_raising(specifier):
    assert is_valid_compatibility(specifier) is False
    assert plugin_compatible(specifier, "1.0.0") is False
    status = compatibility_status(specifier)
    assert status["valid"] is False
    assert status["compatible"] is False


def test_parse_invalid_specifier_raises():
    with pytest.raises(InvalidCompatibilityError):
        parse_compatibility(">>=1.0.0")


@pytest.mark.parametrize(
    ("specifier", "valid"),
    [(">=1.0.0", True), ("", True), (None, True), ("bogus", False)],
)
def test_validity_matrix(specifier, valid):
    assert is_valid_compatibility(specifier) is valid
