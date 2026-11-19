"""Phase 10: cost figures are labelled as estimates in the UI (#443).

Every cost shown to a user must be labelled as an estimate and carry a
tooltip explaining how it was derived. Provider-reported (measured)
amounts are labelled separately so an estimate is never presented as an
exact charge.
"""

from datetime import UTC, datetime

from app.extensions import db
from app.models import UsageRecord, User, Workspace, WorkspaceMember

#: Shared tooltip marker for estimated cost figures.
ESTIMATE_TOOLTIP = (
    "Estimated from recorded token counts and the configured price table; "
    "not a provider bill or an exact charge."
)

#: Tooltip marker for provider-reported (measured) amounts.
REPORTED_TOOLTIP = (
    "Imported from the provider's billing CSV — a provider-reported amount, "
    "not calculated by this app."
)


def _usage(user, *, cost=0.01, tokens=100):
    record = UsageRecord(
        user_id=user.id,
        source="chat",
        provider="mock",
        model="mock-1",
        total_tokens=tokens,
        cost_estimate=cost,
        created_at=datetime.now(UTC),
    )
    db.session.add(record)
    db.session.commit()
    return record


class TestBillingUsagePageLabels:
    def test_costs_are_labelled_with_estimate_tooltips(self, client, login, make_user):
        user = make_user()
        _usage(user)
        login("tester@example.com")
        response = client.get("/billing/usage")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        # Every cost column and metric is labelled as an estimate.
        assert html.count("Estimated cost") >= 3
        assert html.count(f'<abbr title="{ESTIMATE_TOOLTIP}">Estimated cost</abbr>') >= 3
        # The page states the estimate basis in prose as well.
        assert "<strong>estimate</strong>" in html
        # No cost figure is presented as exact.
        assert "Total cost" not in html
        assert "Exact cost" not in html


class TestBillingReconciliationPageLabels:
    def test_budget_and_reconciliation_costs_are_labelled(self, client, login, make_user, app):
        user = make_user()
        _usage(user)
        from app.services import budgets

        budgets.upsert_budget(scope="user", scope_id=user.id, token_limit=1000, cost_limit=1.0)
        from app.models import ProviderBillingRecord

        db.session.add(
            ProviderBillingRecord(
                user_id=user.id, provider="mock", period="2026-11", amount_usd=0.05
            )
        )
        db.session.commit()
        login("tester@example.com")
        response = client.get("/billing/")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        # Budget usage column is labelled as an estimate, with a tooltip.
        assert f'<abbr title="{ESTIMATE_TOOLTIP}">Cost (est.)</abbr>' in html
        # The per-row usage hint repeats the estimate basis.
        assert f'title="{ESTIMATE_TOOLTIP}">estimated</span>' in html

        # Provider-reported amounts are labelled as reported, not estimated.
        assert f'<abbr title="{REPORTED_TOOLTIP}">Reported total</abbr>' in html
        assert f'<abbr title="{REPORTED_TOOLTIP}">Reported (USD)</abbr>' in html
        assert f'<abbr title="{ESTIMATE_TOOLTIP}">Estimated (USD)</abbr>' in html
        # Estimates are never shown as exact amounts.
        assert "Exact" not in html


class TestAdminUsageReportLabels:
    def test_admin_report_costs_are_labelled_with_estimate_tooltips(self, client, login, db):
        admin = User(username="admin", email="admin@example.com", is_admin=True)
        admin.set_password("supersecret123")
        db.session.add(admin)
        db.session.commit()
        _usage(admin)

        login("admin@example.com")
        response = client.get("/admin/usage/report")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        # The totals metric and the table column both carry the estimate label.
        assert html.count(f'<abbr title="{ESTIMATE_TOOLTIP}">Estimated cost</abbr>') >= 2
        assert "<strong>estimate</strong>" in html


class TestWorkspaceUsageLabels:
    def test_members_page_cost_columns_are_labelled(self, client, login, db):
        owner = User(username="owner", email="owner@example.com")
        owner.set_password("supersecret123")
        db.session.add(owner)
        db.session.commit()
        workspace = Workspace(user_id=owner.id, name="Team")
        db.session.add(workspace)
        db.session.commit()
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=owner.id,
                role="owner",
                status="active",
            )
        )
        db.session.commit()

        login("owner@example.com")
        response = client.get(f"/workspaces/{workspace.id}/members")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

        # All three breakdown tables label their cost column as an estimate.
        assert html.count(f'<abbr title="{ESTIMATE_TOOLTIP}">Cost (est.)</abbr>') == 3
        # The usage note states the estimate basis and carries a tooltip.
        assert "Costs are estimates." in html
        assert f'title="{ESTIMATE_TOOLTIP}"' in html
