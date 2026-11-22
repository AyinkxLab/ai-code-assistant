"""Organization overview page (#626)."""

from __future__ import annotations

from flask import render_template, request
from flask_login import login_required

from app.admin import bp
from app.admin.organizations_common import resolve_organization
from app.api.error_catalog import INVALID_INPUT, error_response
from app.services import organizations
from app.services.currency import get_currency_config


@bp.route("/organizations/<int:workspace_id>")
@login_required
def organization_dashboard(workspace_id: int):
    """Single organization overview for owners and admins."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error
    try:
        days = organizations.normalize_days(request.args.get("days"))
    except ValueError as exc:
        return error_response(INVALID_INPUT, str(exc))
    return render_template(
        "admin/organization_dashboard.html",
        workspace=workspace,
        data=organizations.dashboard(workspace, days=days),
        currency=get_currency_config(),
    )
