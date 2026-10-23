"""Regression checks for the plugin contribution policy (#427).

The contributing guide must describe the signed, allowlisted catalog model and
must never reintroduce the obsolete blanket "local only" rule. These checks make
the policy testable and keep a future edit from silently reverting it.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _contributing() -> str:
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").lower()
    return " ".join(text.split())


def test_contributing_has_no_blanket_local_only_rule():
    text = _contributing()

    for stale in ("local only", "local-only", "local/trusted"):
        assert stale not in text, f"CONTRIBUTING.md still contains the stale rule {stale!r}"


def test_contributing_describes_signed_allowlisted_installs():
    text = _contributing()

    for required in (
        "signed and allowlisted",
        "plugin_catalog_file",
        "plugin_marketplace_url",
        "plugin_trust_policy",
        "ed25519",
        "integrity",
        "never accept a client-supplied artifact url",
    ):
        assert required in text, f"CONTRIBUTING.md is missing the plugin policy detail {required!r}"


def test_contributing_keeps_no_implicit_grants_and_no_code_execution():
    text = _contributing()

    assert "never grant capabilities implicitly" in text
    assert "never make the management api/cli load" in text
    assert "or execute plugin code" in text
