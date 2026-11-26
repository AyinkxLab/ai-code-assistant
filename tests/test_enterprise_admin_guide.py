"""The enterprise admin guide documents real routes and commands (#640)."""

from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "enterprise-admin-guide.md"

_ROUTE_RE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[^\s`|]*)")
_STOP = ".,;:)"


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def _normalize(path: str) -> str:
    """Reduce converter/placeholder segments so rules and docs compare equal."""
    path = path.split("?", 1)[0].strip().rstrip(_STOP)
    return re.sub(r"<[^>]+>", "<>", path)


def _documented_routes() -> set[str]:
    return {
        _normalize(match.group(1))
        for match in _ROUTE_RE.finditer(_guide())
        if not match.group(1).startswith("//")
    }


def _registered_routes(app) -> set[str]:
    return {_normalize(rule.rule) for rule in app.url_map.iter_rules()}


def test_every_documented_route_is_registered(app):
    documented = _documented_routes()
    assert documented, "the guide no longer documents any routes"
    missing = documented - _registered_routes(app)
    assert not missing, f"documented routes are not registered: {sorted(missing)}"


def _cli(app, *args: str):
    return CliRunner().invoke(app.cli, list(args))


def test_documented_cli_commands_and_options_exist(app):
    assert "db" in app.cli.commands
    assert "upgrade" in app.cli.commands["db"].commands

    audit = app.cli.commands["audit"]
    assert {"verify", "retention", "purge"} <= set(audit.commands)
    assert {"show", "set"} <= set(audit.commands["retention"].commands)
    assert "purge" in app.cli.commands["usage"].commands

    documented = [
        (("audit", "verify"), ("--workspace-id", "--json")),
        (("audit", "retention", "show"), ("--workspace-id",)),
        (
            ("audit", "retention", "set"),
            ("--workspace-id", "--days", "--legal-hold", "--no-legal-hold"),
        ),
        (("audit", "purge"), ("--workspace-id", "--dry-run")),
        (("usage", "purge"), ("--days", "--dry-run")),
    ]
    for command, options in documented:
        result = _cli(app, *command, "--help")
        assert result.exit_code == 0, result.output
        for option in options:
            assert option in result.output, f"{' '.join(command)} lacks {option}"


def test_guide_is_linked_from_readme_and_admin_ui():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/enterprise-admin-guide.md" in readme

    for template in (
        ROOT / "app" / "templates" / "base.html",
        ROOT / "app" / "templates" / "admin" / "organization_dashboard.html",
    ):
        assert "enterprise-admin-guide.md" in template.read_text(encoding="utf-8")


def test_guide_links_existing_docs_and_describes_minimal_scim():
    text = _guide()

    for target in (
        "organization-admin.md",
        "team-collaboration.md",
        "sso-domain-verification.md",
        "audit-compliance-guide.md",
        "audit-retention.md",
        "user-data-export.md",
        "user-data-deletion.md",
        "sso-scim-setup.md",
        "session-policy.md",
    ):
        assert f"]({target})" in text, f"guide does not link {target}"

    assert "/scim/v2/Groups" in text
    assert "/api/session-policy" in text
    assert "viewer" in text
    assert "delete my data" in text
    assert "3650" in text
