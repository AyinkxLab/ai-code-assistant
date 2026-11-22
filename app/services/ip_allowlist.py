"""Per-organization IP allowlist (#620).

Validates CIDR input with the standard library, stores one configuration per
workspace, and enforces it in a request guard. The guard only applies to routes
that resolve a workspace (directly via ``workspace_id`` or through a
``project_id``), and it fails closed: an enabled allowlist with no usable
entries, an unparseable client address, or an address outside every range is
denied.

Exemptions are limited to endpoints that must keep working before a workspace is
known: static assets, the health check, ``/metrics`` (has its own auth), and the
favicon. See ``docs/ip-allowlist.md``.
"""

from __future__ import annotations

import ipaddress
import json

from flask import render_template, request

from app.api.error_catalog import FORBIDDEN, error_response, wants_json_error
from app.extensions import db
from app.models import Project, WorkspaceIPAllowlist
from app.services import audit

#: Hard cap so a configuration cannot grow without bound.
MAX_CIDRS = 100

#: Path prefixes the guard never enforces. Auth and workspace resolution are
#: not available for these, and they must keep working for probes and the login
#: page assets.
EXEMPT_PREFIXES = ("/static/", "/health", "/healthz", "/metrics", "/favicon.ico")


class AllowlistError(ValueError):
    """Invalid allowlist configuration."""


def normalize_cidrs(values) -> list[str]:
    """Validate and canonicalize a list of CIDR strings.

    Single addresses become ``/32`` (IPv4) or ``/128`` (IPv6). Host bits are
    masked to the network address. Duplicates are removed while preserving
    order. Raises :class:`AllowlistError` for malformed or non-list input.
    """
    if not isinstance(values, (list, tuple)):
        raise AllowlistError("cidrs must be a list of CIDR strings.")
    if len(values) > MAX_CIDRS:
        raise AllowlistError(f"cidrs may contain at most {MAX_CIDRS} entries.")
    normalized: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise AllowlistError("Each CIDR entry must be a non-empty string.")
        try:
            network = ipaddress.ip_network(raw.strip(), strict=False)
        except ValueError as exc:
            raise AllowlistError(f"Invalid CIDR: {raw!r}.") from exc
        canonical = str(network)
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def get_config(workspace_id: int) -> WorkspaceIPAllowlist | None:
    """Return the stored allowlist row, or ``None`` when not configured."""
    return WorkspaceIPAllowlist.query.filter_by(workspace_id=workspace_id).first()


def is_enabled(workspace_id: int) -> bool:
    """Return ``True`` when the workspace has an enabled allowlist."""
    row = get_config(workspace_id)
    return bool(row is not None and row.enabled)


def is_allowed(workspace_id: int, client_ip: str | None) -> bool:
    """Return ``True`` when ``client_ip`` may access the workspace.

    Disabled or missing configuration allows every address. Enabled
    configuration fails closed: no entries, an invalid client address, or no
    matching network denies access.
    """
    row = get_config(workspace_id)
    if row is None or not row.enabled:
        return True
    cidrs = row.parsed_cidrs()
    if not cidrs or not client_ip:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return True
    return False


def set_config(
    workspace_id: int,
    *,
    enabled: bool,
    cidrs=None,
    user=None,
    commit: bool = True,
) -> WorkspaceIPAllowlist:
    """Create or update a workspace allowlist and audit the change."""
    if not isinstance(enabled, bool):
        raise AllowlistError("enabled must be a boolean.")
    row = get_config(workspace_id)
    if cidrs is None:
        normalized = row.parsed_cidrs() if row is not None else []
    else:
        normalized = normalize_cidrs(cidrs)
    if enabled and not normalized:
        raise AllowlistError("An enabled allowlist needs at least one CIDR range.")

    previous = {"enabled": bool(row.enabled), "cidrs": row.parsed_cidrs()} if row else None
    if row is None:
        row = WorkspaceIPAllowlist(workspace_id=workspace_id)
        db.session.add(row)
    row.enabled = enabled
    row.cidrs = json.dumps(normalized)
    db.session.flush()
    audit.record(
        audit.IP_ALLOWLIST_UPDATED,
        workspace_id=workspace_id,
        user=user,
        target_type="workspace_ip_allowlist",
        target_id=row.id,
        metadata={
            "enabled": enabled,
            "cidrs": normalized,
            "previous": previous,
        },
    )
    if commit:
        db.session.commit()
    return row


def _workspace_id_for_request() -> int | None:
    """Resolve the target workspace from the matched route arguments."""
    view_args = request.view_args or {}
    workspace_id = view_args.get("workspace_id")
    if workspace_id is not None:
        try:
            return int(workspace_id)
        except (TypeError, ValueError):
            return None
    project_id = view_args.get("project_id")
    if project_id is not None:
        try:
            project = db.session.get(Project, int(project_id))
        except (TypeError, ValueError):
            return None
        return project.workspace_id if project is not None else None
    return None


def _is_exempt(path: str) -> bool:
    return path.startswith(EXEMPT_PREFIXES)


def enforce_request():
    """Request guard: deny a blocked client before the view runs.

    Returns ``None`` when the request may continue, otherwise a structured JSON
    error (API paths) or the rendered 403 page (HTML paths).
    """
    if _is_exempt(request.path):
        return None
    workspace_id = _workspace_id_for_request()
    if workspace_id is None:
        return None
    if is_allowed(workspace_id, request.remote_addr):
        return None
    if wants_json_error():
        return error_response(
            FORBIDDEN, "Access from your IP address is not allowed for this organization."
        )
    return render_template("errors/403.html"), 403


def init_ip_allowlist(app) -> None:
    """Register the allowlist guard on ``app``."""
    app.before_request(enforce_request)


__all__ = [
    "EXEMPT_PREFIXES",
    "MAX_CIDRS",
    "AllowlistError",
    "enforce_request",
    "get_config",
    "init_ip_allowlist",
    "is_allowed",
    "is_enabled",
    "normalize_cidrs",
    "set_config",
]
