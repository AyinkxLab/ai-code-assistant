"""Provider billing reconciliation routes (Phase 10).

Every endpoint is scoped to the signed-in user: their provider-reported imports
are reconciled against the estimate derived from *their* token usage. Imports
only ever write :class:`~app.models.provider_billing_record.ProviderBillingRecord`
rows, so the estimate is never overwritten.
"""

from __future__ import annotations

from flask import Response, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.billing import bp
from app.extensions import db
from app.models import Budget, Workspace
from app.services import audit, billing_reconciliation, budgets, permissions, usage_dashboards
from app.services.billing_reconciliation import BillingImportError
from app.services.budget_access import can_access_workspace_billing
from app.services.invoices import InvoiceError, generate_invoice


def _budget_workspaces():
    """Workspaces whose budgets the caller can manage.

    Global admins may manage any workspace; everyone else is limited to the
    workspaces they own (the only role with ``manage_billing`` today).
    """
    query = Workspace.query
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)
    return query.order_by(Workspace.name).all()


def _workspace_for_budget(workspace_id: int):
    """Resolve a workspace for budget management, allowing global admins."""
    if current_user.is_admin:
        workspace = db.session.get(Workspace, workspace_id)
        if workspace is None:
            abort(404)
        return workspace
    return permissions.resolve_workspace(workspace_id, current_user)


def _can_manage_budget(workspace) -> bool:
    return bool(current_user.is_admin) or permissions.can(
        "manage_billing", workspace.id, current_user
    )


def _with_scope_names(rows: list[dict]) -> list[dict]:
    """Attach a human-readable scope name to each budget status row."""
    names = {workspace.id: workspace.name for workspace in _budget_workspaces()}
    for row in rows:
        if row.get("scope") == "workspace":
            row["scope_name"] = names.get(row.get("scope_id"), f"Workspace #{row.get('scope_id')}")
        else:
            row["scope_name"] = "My account"
    return rows


def _wants_json() -> bool:
    if request.args.get("format") == "json":
        return True
    return request.accept_mimetypes.best == "application/json"


def _extract_csv() -> tuple[str | None, str | None]:
    """Return ``(text, filename)`` from the request, or ``(None, error)``."""
    uploaded = request.files.get("file")
    if uploaded is not None and uploaded.filename:
        try:
            raw = uploaded.read()
        except OSError as exc:
            return None, f"Could not read the uploaded file: {exc}"
        try:
            return raw.decode("utf-8"), uploaded.filename
        except UnicodeDecodeError:
            return None, "The CSV must be UTF-8 encoded text."

    text = request.form.get("csv")
    if text is None:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            text = payload.get("csv")
    if isinstance(text, str) and text.strip():
        return text, None
    return None, "No CSV was provided."


@bp.route("/")
@login_required
def index():
    """Render the reconciliation page for the signed-in user."""
    report = billing_reconciliation.reconciliation_report(user_id=current_user.id)
    managed_workspaces = _budget_workspaces()
    budget_rows = _with_scope_names(
        budgets.statuses_for_user(current_user, [workspace.id for workspace in managed_workspaces])
    )
    return render_template(
        "billing/index.html",
        report=report,
        budgets=budget_rows,
        workspaces=managed_workspaces,
    )


@bp.route("/import", methods=["POST"])
@login_required
def import_billing():
    """Import a provider billing CSV and reconcile it against estimates.

    Accepts a ``multipart/form-data`` upload in the ``file`` field, a ``csv``
    form field, or a ``{"csv": "..."}`` JSON body. The estimate is recomputed
    from token usage and never overwritten by the import.
    """
    text, filename = _extract_csv()
    if text is None:
        if _wants_json():
            return jsonify({"error": filename, "kind": "invalid"}), 400
        return redirect(url_for("billing.index"))

    try:
        result = billing_reconciliation.import_billing_csv(
            text, user_id=current_user.id, source_filename=filename
        )
    except BillingImportError as exc:
        if _wants_json():
            return jsonify({"error": str(exc), "kind": exc.kind}), 400
        return redirect(url_for("billing.index"))

    audit.record(
        audit.PROVIDER_BILLING_IMPORTED,
        user=current_user,
        target_type="provider_billing",
        metadata={
            "created": result["created"],
            "updated": result["updated"],
            "rows": result["rows"],
            "source_filename": filename,
        },
    )
    db.session.commit()

    if _wants_json():
        return jsonify(result)
    return redirect(url_for("billing.index"))


@bp.route("/api/reconciliation")
@login_required
def api_reconciliation():
    """Return the signed-in user's reconciliation report as JSON."""
    period = request.args.get("period")
    try:
        report = billing_reconciliation.reconciliation_report(
            user_id=current_user.id, period=period or None
        )
    except BillingImportError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 400
    return jsonify(report)


def _json_payload() -> dict:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return request.form.to_dict()


def _budget_status(budget: Budget) -> dict:
    return budgets.evaluate_budget(budget).to_dict()


@bp.route("/api/budgets", methods=["GET"])
@login_required
def api_list_budgets():
    """List the caller's user budget and workspace budgets they can manage."""
    managed = _budget_workspaces()
    rows = _with_scope_names(
        budgets.statuses_for_user(current_user, [workspace.id for workspace in managed])
    )
    return jsonify(
        {
            "budgets": rows,
            "workspaces": [{"id": workspace.id, "name": workspace.name} for workspace in managed],
        }
    )


@bp.route("/api/budgets", methods=["PUT", "POST"])
@login_required
def api_upsert_budget():
    """Create or replace a monthly user/workspace budget.

    A workspace budget is owner-only (``manage_billing``); a user budget is
    scoped to the caller. Both token and cost limits are optional, but at least
    one must be provided.
    """
    payload = _json_payload()
    scope = (payload.get("scope") or "user").strip().lower()

    if scope == "workspace":
        try:
            workspace_id = int(payload.get("workspace_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "workspace_id is required.", "kind": "invalid"}), 400
        workspace = _workspace_for_budget(workspace_id)
        if not _can_manage_budget(workspace):
            return jsonify({"error": "Forbidden.", "kind": "forbidden"}), 403
        scope_id = workspace.id
    elif scope == "user":
        scope_id = current_user.id
    else:
        return jsonify({"error": "scope must be 'user' or 'workspace'.", "kind": "invalid"}), 400

    try:
        budget = budgets.upsert_budget(
            scope=scope,
            scope_id=scope_id,
            token_limit=payload.get("token_limit"),
            cost_limit=payload.get("cost_limit"),
            warn_threshold=payload.get("warn_threshold"),
            period=payload.get("period"),
            actor=current_user,
        )
    except budgets.BudgetValidationError as exc:
        return jsonify({"error": str(exc), "kind": "invalid"}), 400

    row = _with_scope_names([_budget_status(budget)])[0]
    return jsonify({"budget": row}), 200


@bp.route("/api/budgets/<int:budget_id>", methods=["DELETE"])
@login_required
def api_delete_budget(budget_id: int):
    """Delete a budget the caller owns or may manage."""
    budget = db.session.get(Budget, budget_id)
    if budget is None:
        return jsonify({"error": "Budget not found.", "kind": "not_found"}), 404

    if budget.user_id is not None:
        if budget.user_id != current_user.id:
            return jsonify({"error": "Forbidden.", "kind": "forbidden"}), 403
    else:
        workspace = _workspace_for_budget(budget.workspace_id)
        if not _can_manage_budget(workspace):
            return jsonify({"error": "Forbidden.", "kind": "forbidden"}), 403

    metadata = {"scope": budget.scope, "scope_id": budget.scope_id}
    budgets.delete_budget(budget, commit=False)
    audit.record(
        audit.BUDGET_DELETED,
        user=current_user,
        target_type="budget",
        target_id=budget_id,
        metadata=metadata,
    )
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Usage and cost dashboards (#368)
# ---------------------------------------------------------------------------


def _parse_days():
    """Parse ``days`` and return ``(days, error_response)``."""
    try:
        return usage_dashboards.normalize_days(request.args.get("days")), None
    except ValueError as exc:
        return None, (jsonify({"error": str(exc), "kind": "invalid"}), 400)


def _dashboard_refusal():
    return jsonify({"error": "Usage dashboard not found.", "kind": "not_found"}), 404


def _csv_response(text: str, truncated: bool, filename: str) -> Response:
    response = Response(text, mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    if truncated:
        response.headers["X-Export-Truncated"] = "true"
    return response


@bp.route("/usage")
@login_required
def usage_page():
    """Render the signed-in user's dashboard (optionally a workspace view)."""
    days, _error = _parse_days()
    if days is None:
        days = usage_dashboards.DEFAULT_WINDOW_DAYS
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is not None and not can_access_workspace_billing(workspace_id, current_user):
        abort(404)
    payload = usage_dashboards.dashboard(
        user_id=current_user.id, workspace_id=workspace_id, days=days
    )
    workspaces = (
        Workspace.query.filter_by(user_id=current_user.id)
        .order_by(Workspace.name, Workspace.id)
        .all()
    )
    return render_template(
        "billing/usage.html",
        dashboard=payload,
        days=days,
        workspace_id=workspace_id,
        workspaces=workspaces,
    )


@bp.route("/api/usage/dashboard")
@login_required
def api_usage_dashboard():
    """Return the signed-in user's usage/cost dashboard as JSON."""
    days, error = _parse_days()
    if error is not None:
        return error
    return jsonify(usage_dashboards.dashboard(user_id=current_user.id, days=days))


@bp.route("/api/usage/dashboard.csv")
@login_required
def api_usage_dashboard_csv():
    """Download the signed-in user's usage ledger as bounded CSV."""
    days, error = _parse_days()
    if error is not None:
        return error
    text, truncated = usage_dashboards.export_csv(user_id=current_user.id, days=days)
    return _csv_response(text, truncated, "usage.csv")


@bp.route("/api/workspaces/<int:workspace_id>/usage")
@login_required
def api_workspace_usage(workspace_id: int):
    """Return a workspace dashboard for its owner or a site administrator."""
    if not can_access_workspace_billing(workspace_id, current_user):
        return _dashboard_refusal()
    days, error = _parse_days()
    if error is not None:
        return error
    return jsonify(usage_dashboards.dashboard(workspace_id=workspace_id, days=days))


@bp.route("/api/workspaces/<int:workspace_id>/usage.csv")
@login_required
def api_workspace_usage_csv(workspace_id: int):
    """Download a workspace ledger as bounded CSV (owner/admin only)."""
    if not can_access_workspace_billing(workspace_id, current_user):
        return _dashboard_refusal()
    days, error = _parse_days()
    if error is not None:
        return error
    text, truncated = usage_dashboards.export_csv(workspace_id=workspace_id, days=days)
    return _csv_response(text, truncated, f"workspace-{workspace_id}-usage.csv")


# ---------------------------------------------------------------------------
# Monthly invoice generation (#482)
# ---------------------------------------------------------------------------


def _invoice_for(scope: str, scope_id: int, period: str):
    """Generate an invoice and return a JSON response, or an error response."""
    try:
        invoice = generate_invoice(scope=scope, scope_id=scope_id, period=period)
    except InvoiceError as exc:
        return jsonify({"error": str(exc), "kind": exc.kind}), 400
    return jsonify(invoice)


@bp.route("/api/invoices")
@login_required
def api_list_invoices():
    """Return a draft invoice for the signed-in user for a billing period.

    Query parameters
    ----------------
    period : str
        Required. A ``YYYY-MM`` calendar month, e.g. ``2026-10``.
    workspace_id : int, optional
        When supplied the invoice is scoped to that workspace instead of the
        caller's user account. The caller must own the workspace or be a site
        administrator.

    Returns
    -------
    200
        JSON invoice document (``draft: true``).
    400
        ``{"error": "...", "kind": "..."}`` when the period is missing or invalid.
    403 / 404
        When ``workspace_id`` is provided but the caller cannot access it.
    """
    period = request.args.get("period", "").strip()
    if not period:
        return jsonify({"error": "period is required.", "kind": "invalid"}), 400

    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is not None:
        if not can_access_workspace_billing(workspace_id, current_user):
            return jsonify({"error": "Invoice not found.", "kind": "not_found"}), 404
        return _invoice_for("workspace", workspace_id, period)

    return _invoice_for("user", current_user.id, period)


@bp.route("/api/invoices/<period>")
@login_required
def api_get_invoice(period: str):
    """Return a draft invoice for the signed-in user for ``<period>``.

    The period must be ``YYYY-MM``. An optional ``workspace_id`` query parameter
    scopes the invoice to a workspace the caller can access.

    Returns
    -------
    200
        JSON invoice document (``draft: true``).
    400
        When the period string is malformed.
    404
        When ``workspace_id`` is provided but the caller cannot access it.
    """
    workspace_id = request.args.get("workspace_id", type=int)
    if workspace_id is not None:
        if not can_access_workspace_billing(workspace_id, current_user):
            return jsonify({"error": "Invoice not found.", "kind": "not_found"}), 404
        return _invoice_for("workspace", workspace_id, period)

    return _invoice_for("user", current_user.id, period)
