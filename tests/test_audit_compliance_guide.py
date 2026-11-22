"""The audit and compliance guide documents real commands/endpoints (#642)."""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

from app.services.evidence_pack import DISCLAIMER

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "audit-compliance-guide.md"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _cli(app, *args: str):
    return CliRunner().invoke(app.cli, list(args))


def test_documented_audit_commands_and_options_exist(app):
    audit = app.cli.commands["audit"]
    assert {"verify", "purge", "retention"} <= set(audit.commands)
    retention = audit.commands["retention"]
    assert {"show", "set"} <= set(retention.commands)

    documented = [
        (("audit", "verify"), ("--workspace-id", "--json")),
        (("audit", "retention", "show"), ("--workspace-id", "--json")),
        (
            ("audit", "retention", "set"),
            ("--workspace-id", "--days", "--legal-hold", "--no-legal-hold"),
        ),
        (("audit", "purge"), ("--workspace-id", "--dry-run")),
    ]
    for command, options in documented:
        result = _cli(app, *command, "--help")
        assert result.exit_code == 0, result.output
        for option in options:
            assert option in result.output, f"{' '.join(command)} lacks {option}"


def test_documented_endpoints_are_registered(app):
    rules = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/admin/audit/export" in rules
    assert "/admin/actions" in rules
    assert any(
        rule.startswith("/workspaces/api/workspaces/") and rule.endswith("/audit") for rule in rules
    ), "workspace audit API is not registered"


def test_guide_links_the_existing_audit_docs_and_keeps_the_disclaimer():
    text = _guide()

    for target in (
        "audit-integrity.md",
        "audit-retention.md",
        "compliance-evidence.md",
        "data-classification.md",
        "admin-actions.md",
    ):
        assert target in text, f"guide does not link {target}"

    assert "not a certification, attestation, or audit opinion" in DISCLAIMER
    assert "not a certification, attestation, or audit opinion" in text
    assert "3650" in text
    assert "legal_hold" in text


def test_every_documented_flask_audit_command_is_documented_in_the_cli_help(app):
    """Every `flask audit ...` command in the guide resolves to a real command."""
    commands = set(re.findall(r"flask audit ([a-z-]+)", _guide()))
    assert commands, "the guide no longer documents any audit commands"
    audit = app.cli.commands["audit"]
    assert commands <= set(audit.commands), commands - set(audit.commands)
