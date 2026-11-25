"""Read-only effective capability preview for the organization admin console (#602).

The preview resolves the three role layers that already exist in this codebase:

* organization role (``owner``/``admin``) from ``organizations.py``;
* team role (the active workspace membership row);
* workspace role (``Workspace.user_id``).

It never writes assignments. Capabilities stay in their existing namespaces:
workspace permissions use plain names from ``permissions.py``, organization
permissions use their own small catalog, and plugin capabilities keep the
``scope:action`` strings from ``capabilities.py``. Capabilities that come from
more than one layer list every source rather than being flattened into one
string.
"""

from __future__ import annotations

from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_OWNER, ROLE_VIEWER, STATUS_ACTIVE
from app.services import capabilities as plugin_capabilities
from app.services import organizations, permissions

SOURCE_WORKSPACE_OWNER = "workspace_owner"
SOURCE_TEAM_ROLE = "team_role"
SOURCE_ORGANIZATION_ROLE = "organization_role"
SOURCE_SIMULATED_ROLE = "simulated_role"

#: Roles an admin may preview. ``owner`` is included for ownership-transfer
#: previews; it is never assigned by this endpoint.
SIMULATABLE_ROLES = (ROLE_VIEWER, ROLE_CONTRIBUTOR, organizations.ORG_ROLE_ADMIN, ROLE_OWNER)


def _active_membership(workspace_id: int, user_id: int) -> WorkspaceMember | None:
    return WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user_id).first()


def _entry(namespace: str, capability: str, description: str) -> dict:
    return {
        "namespace": namespace,
        "capability": capability,
        "description": description,
        "sources": [],
    }


def _add_source(entry: dict, source: str, role: str) -> None:
    if not any(item["source"] == source and item["role"] == role for item in entry["sources"]):
        entry["sources"].append({"source": source, "role": role})


def simulate_capabilities(
    workspace: Workspace,
    target_user: User,
    role_override: str | None = None,
) -> dict:
    """Return effective capabilities for ``target_user`` without mutating roles.

    ``role_override`` only changes the calculated workspace/plugin role; the
    database is never touched. Organization role is always the real role.
    """
    organization_role = organizations.organization_role(workspace.id, target_user)
    membership = _active_membership(workspace.id, target_user.id)
    team_role = (
        membership.role if membership is not None and membership.status == STATUS_ACTIVE else None
    )
    workspace_role = ROLE_OWNER if workspace.user_id == target_user.id else None

    if role_override is not None:
        effective_role = role_override
        workspace_sources = [(role_override, SOURCE_SIMULATED_ROLE)]
    elif workspace_role is not None:
        effective_role = workspace_role
        workspace_sources = [(workspace_role, SOURCE_WORKSPACE_OWNER)]
    elif team_role is not None:
        effective_role = team_role
        workspace_sources = [(team_role, SOURCE_TEAM_ROLE)]
    else:
        effective_role = None
        workspace_sources = []

    workspace_entries: dict[str, dict] = {}
    for role, source in workspace_sources:
        for capability in permissions.capabilities_for_role(role):
            entry = workspace_entries.setdefault(
                capability,
                _entry("workspace", capability, permissions.CAPABILITIES[capability][1]),
            )
            _add_source(entry, source, role)

    organization_entries: dict[str, dict] = {}
    for capability in organizations.capabilities_for_organization_role(organization_role):
        description = organizations.ORGANIZATION_CAPABILITIES[capability][1]
        entry = organization_entries.setdefault(
            capability, _entry("organization", capability, description)
        )
        _add_source(entry, SOURCE_ORGANIZATION_ROLE, organization_role)

    plugin_sources = list(workspace_sources)
    if organization_role in organizations.ORG_ROLES:
        plugin_sources.append((organization_role, SOURCE_ORGANIZATION_ROLE))
    plugin_entries: dict[str, dict] = {}
    for role, source in plugin_sources:
        for capability in plugin_capabilities.CapabilityStore.get_role_capabilities(role):
            enum_member = plugin_capabilities.Capability.from_string(capability)
            description = enum_member.name.replace("_", " ").title() if enum_member else capability
            entry = plugin_entries.setdefault(capability, _entry("plugin", capability, description))
            _add_source(entry, source, role)

    def _sorted(entries: dict[str, dict]) -> list[dict]:
        return [entries[key] for key in sorted(entries)]

    return {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "user_id": target_user.id,
        "username": target_user.username,
        "simulated": role_override is not None,
        "simulated_role": role_override,
        "roles": {
            "organization": organization_role,
            "team": team_role,
            "workspace": workspace_role,
            "effective": effective_role,
        },
        "membership_id": membership.id if membership is not None else None,
        "capabilities": {
            "workspace": _sorted(workspace_entries),
            "organization": _sorted(organization_entries),
            "plugin": _sorted(plugin_entries),
        },
        "capability_count": (
            len(workspace_entries) + len(organization_entries) + len(plugin_entries)
        ),
    }
