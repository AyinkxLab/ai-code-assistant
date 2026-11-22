"""Runtime capability enforcement tests (#688)."""

from __future__ import annotations

import pytest

from app.models import AuditLog, CapabilityGrant, Plugin, PluginInstallation, Workspace
from app.services.capabilities import CapabilityDeniedError, require_capability

CAPABILITY = "project:read"


def _setup(db, make_user, *, installation=True, enabled=True, grant=True, granted=None):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    plugin = Plugin(
        id="demo-plugin",
        name="Demo",
        version="1.0.0",
        entry_point="demo.plugin:Plugin",
    )
    db.session.add(plugin)
    db.session.flush()
    if installation:
        db.session.add(
            PluginInstallation(
                plugin_id=plugin.id,
                workspace_id=workspace.id,
                enabled=enabled,
                granted_capabilities=granted if granted is not None else [CAPABILITY],
            )
        )
    if grant:
        db.session.add(
            CapabilityGrant(
                plugin_id=plugin.id,
                workspace_id=workspace.id,
                capability=CAPABILITY,
            )
        )
    db.session.commit()
    return plugin, workspace


def test_granted_capability_passes(db, make_user):
    plugin, workspace = _setup(db, make_user)
    require_capability(plugin.id, workspace.id, CAPABILITY)


def test_revoked_capability_fails_and_is_audited(db, make_user):
    plugin, workspace = _setup(db, make_user)
    grant = CapabilityGrant.query.filter_by(plugin_id=plugin.id, workspace_id=workspace.id).one()
    db.session.delete(grant)
    db.session.commit()

    with pytest.raises(CapabilityDeniedError) as excinfo:
        require_capability(plugin.id, workspace.id, CAPABILITY)
    assert excinfo.value.code == "capability_denied"
    assert excinfo.value.to_dict()["capability"] == CAPABILITY
    assert AuditLog.query.filter_by(action="plugin.capability.denied").count() == 1


def test_missing_installation_fails_closed(db, make_user):
    plugin, workspace = _setup(db, make_user, installation=False)
    with pytest.raises(CapabilityDeniedError):
        require_capability(plugin.id, workspace.id, CAPABILITY)


def test_disabled_installation_fails_closed(db, make_user):
    plugin, workspace = _setup(db, make_user, enabled=False)
    with pytest.raises(CapabilityDeniedError):
        require_capability(plugin.id, workspace.id, CAPABILITY)


def test_installation_without_capability_fails_closed(db, make_user):
    plugin, workspace = _setup(db, make_user, granted=[])
    with pytest.raises(CapabilityDeniedError):
        require_capability(plugin.id, workspace.id, CAPABILITY)


def test_invalid_capability_fails_closed(db, make_user):
    plugin, workspace = _setup(db, make_user)
    with pytest.raises(CapabilityDeniedError):
        require_capability(plugin.id, workspace.id, "not:a:capability")
