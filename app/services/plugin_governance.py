"""Organization plugin governance (#634).

Policy modes are enforced at install time by every workspace install route:

* ``allow`` — installs proceed as before;
* ``block`` — every install is refused and audited;
* ``require_approval`` — installs are refused until an organization admin
  approves that plugin id.

Only organization admins (the workspace owner or an active ``owner``/``admin``
member, via :mod:`app.services.organizations`) may change the policy or approve
a plugin. Policy changes, approvals, and refused installs append audit rows.
"""

from __future__ import annotations

from app.extensions import db
from app.models import OrganizationPluginPolicy, PluginApproval, Workspace
from app.models.organization_plugin_policy import (
    MAX_PLUGIN_ID_LENGTH,
    MODE_ALLOW,
    MODE_BLOCK,
    MODE_REQUIRE_APPROVAL,
    MODES,
)
from app.services import audit
from app.services.organizations import is_organization_admin


class PluginGovernanceError(ValueError):
    """Invalid governance input."""


class PluginBlocked(PluginGovernanceError):
    """The organization blocks this plugin."""


class PluginApprovalRequired(PluginGovernanceError):
    """The organization requires an approval before this plugin may install."""


def _workspace(workspace_id: int) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise LookupError("Workspace not found.")
    return workspace


def _require_admin(workspace_id: int, actor) -> None:
    if actor is not None and not is_organization_admin(workspace_id, actor):
        raise PermissionError("You do not have permission to manage plugin governance.")


def validate_mode(value) -> str:
    """Return a canonical policy mode or raise :class:`PluginGovernanceError`."""
    if not isinstance(value, str) or value.strip().lower() not in MODES:
        raise PluginGovernanceError("mode must be allow, block, or require_approval.")
    return value.strip().lower()


def validate_plugin_id(value) -> str:
    """Return a trimmed plugin id or raise :class:`PluginGovernanceError`."""
    if not isinstance(value, str) or not value.strip():
        raise PluginGovernanceError("plugin_id is required.")
    normalized = value.strip()
    if len(normalized) > MAX_PLUGIN_ID_LENGTH:
        raise PluginGovernanceError(f"plugin_id must be at most {MAX_PLUGIN_ID_LENGTH} characters.")
    return normalized


def _owner_id(workspace_id: int) -> int:
    workspace = _workspace(workspace_id)
    return workspace.user_id


def policy_for(workspace_id: int) -> dict:
    """Return the effective policy for the workspace's organization."""
    owner_id = _owner_id(workspace_id)
    row = OrganizationPluginPolicy.query.filter_by(owner_id=owner_id).first()
    if row is None:
        return {"owner_id": owner_id, "mode": MODE_ALLOW, "updated_by_id": None, "updated_at": None}
    return row.to_dict()


def set_policy(
    workspace_id: int,
    *,
    mode,
    actor=None,
    commit: bool = True,
) -> dict:
    """Create or replace the organization's plugin install policy."""
    owner_id = _owner_id(workspace_id)
    _require_admin(workspace_id, actor)
    normalized = validate_mode(mode)

    row = OrganizationPluginPolicy.query.filter_by(owner_id=owner_id).first()
    if row is None:
        row = OrganizationPluginPolicy(owner_id=owner_id)
        db.session.add(row)
    row.mode = normalized
    row.updated_by_id = getattr(actor, "id", None)
    audit.record(
        audit.PLUGIN_POLICY_UPDATED,
        workspace_id=workspace_id,
        user=actor,
        target_type="organization",
        target_id=owner_id,
        metadata={"mode": normalized},
    )
    if commit:
        db.session.commit()
    return row.to_dict()


def is_approved(workspace_id: int, plugin_id: str) -> bool:
    """Return ``True`` when the organization approved ``plugin_id``."""
    owner_id = _owner_id(workspace_id)
    return (
        PluginApproval.query.filter_by(owner_id=owner_id, plugin_id=plugin_id).first() is not None
    )


def approve_plugin(
    workspace_id: int,
    plugin_id,
    *,
    reason=None,
    actor=None,
    commit: bool = True,
) -> dict:
    """Approve a plugin id for the organization and audit the decision."""
    owner_id = _owner_id(workspace_id)
    _require_admin(workspace_id, actor)
    normalized_id = validate_plugin_id(plugin_id)
    normalized_reason = None
    if reason is not None:
        if not isinstance(reason, str):
            raise PluginGovernanceError("reason must be a string.")
        normalized_reason = reason.strip()[:2000] or None

    row = PluginApproval.query.filter_by(owner_id=owner_id, plugin_id=normalized_id).first()
    if row is None:
        row = PluginApproval(owner_id=owner_id, plugin_id=normalized_id)
        db.session.add(row)
    row.approved_by_id = getattr(actor, "id", None)
    row.reason = normalized_reason
    audit.record(
        audit.PLUGIN_INSTALL_APPROVED,
        workspace_id=workspace_id,
        user=actor,
        target_type="plugin",
        metadata={"plugin_id": normalized_id},
    )
    if commit:
        db.session.commit()
    return row.to_dict()


def enforce_install(workspace_id: int, plugin_id, *, actor=None, commit: bool = True) -> None:
    """Refuse an install the organization policy does not permit.

    A refusal is audited before raising so the blocked attempt survives even
    when the caller abandons its transaction. Raises ``PluginBlocked`` or
    ``PluginApprovalRequired``.
    """
    normalized_id = validate_plugin_id(plugin_id)
    policy = policy_for(workspace_id)
    mode = policy["mode"]
    if mode == MODE_ALLOW:
        return

    if mode == MODE_BLOCK:
        audit.record(
            audit.PLUGIN_INSTALL_BLOCKED,
            workspace_id=workspace_id,
            user=actor,
            target_type="plugin",
            metadata={"plugin_id": normalized_id, "mode": mode},
        )
        if commit:
            db.session.commit()
        raise PluginBlocked("This organization blocks plugin installation.")

    if mode == MODE_REQUIRE_APPROVAL and not is_approved(workspace_id, normalized_id):
        audit.record(
            audit.PLUGIN_INSTALL_BLOCKED,
            workspace_id=workspace_id,
            user=actor,
            target_type="plugin",
            metadata={"plugin_id": normalized_id, "mode": mode},
        )
        if commit:
            db.session.commit()
        raise PluginApprovalRequired(
            "This plugin requires organization approval before it can be installed."
        )


__all__ = [
    "MODES",
    "MODE_ALLOW",
    "MODE_BLOCK",
    "MODE_REQUIRE_APPROVAL",
    "PluginApprovalRequired",
    "PluginBlocked",
    "PluginGovernanceError",
    "approve_plugin",
    "enforce_install",
    "is_approved",
    "policy_for",
    "set_policy",
    "validate_mode",
    "validate_plugin_id",
]
