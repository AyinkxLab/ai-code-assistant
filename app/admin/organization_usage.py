"""Organization usage and budget rollups (#633)."""

from __future__ import annotations

from flask import render_template, request
from flask_login import login_required

from app.admin import bp
from app.admin.organizations_common import resolve_organization
from app.api.error_catalog import INVALID_INPUT, error_response
from app.services import organizations
from app.services.currency import get_currency_config


@bp.route("/organizations/<int:workspace_id>/usage")
@login_required
def organization_usage(workspace_id: int):
    """Organization usage and budget rollups for admins."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error
    try:
        days = organizations.normalize_days(request.args.get("days"))
    except ValueError as exc:
        return error_response(INVALID_INPUT, str(exc))
    return render_template(
        "admin/organization_usage.html",
        workspace=workspace,
        days=days,
        usage=organizations.usage_summary(workspace, days=days),
        budgets=organizations.budget_status(workspace),
        currency=get_currency_config(),
    )
