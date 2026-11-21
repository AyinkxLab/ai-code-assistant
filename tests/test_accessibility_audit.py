"""Tests for the automated accessibility audit (#712)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _audit_module():
    spec = importlib.util.spec_from_file_location(
        "accessibility_audit", REPO_ROOT / "scripts" / "accessibility_audit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_flows_pass_the_static_audit():
    audit = _audit_module()
    assert audit.find_violations(REPO_ROOT) == []


def test_contrast_ratio_for_black_and_white_is_maximal():
    audit = _audit_module()
    assert round(audit.contrast_ratio("#000000", "#ffffff")) == 21
