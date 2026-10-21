"""Tests for monthly invoice generation (Phase 10, issue #482).

Covers:
- Multiple scope types: user and workspace.
- Empty period (no usage rows) returns zero totals with an empty line_items list.
- Line-item accuracy: tokens and costs aggregate correctly per provider/model.
- draft=True is always set.
- API routes: GET /billing/api/invoices and GET /billing/api/invoices/<period>.
- Access control: workspace invoices require ownership/admin access.
- Invalid inputs: bad period, missing period, unknown scope.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.extensions import db
from app.models import UsageRecord, Workspace
from app.services.invoices import InvoiceError, generate_invoice

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace(user_id, name="Test Workspace"):
    ws = Workspace(user_id=user_id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _add_record(
    *,
    user_id,
    workspace_id=None,
    provider="openai",
    model="gpt-4o",
    prompt_tokens=100,
    completion_tokens=50,
    cost_estimate=0.001,
    period="2026-10",
):
    """Insert one UsageRecord for the given period (day 15 of the month)."""
    year, month = (int(p) for p in period.split("-"))
    created = datetime(year, month, 15, 12, 0, 0, tzinfo=UTC)
    record = UsageRecord(
        user_id=user_id,
        workspace_id=workspace_id,
        source="chat",
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        tokens_estimated=False,
        cost_estimate=cost_estimate,
    )
    record.created_at = created
    db.session.add(record)
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Unit tests: generate_invoice service
# ---------------------------------------------------------------------------


class TestGenerateInvoiceEmptyPeriod:
    """An invoice for a period with no usage rows should return zero totals."""

    def test_empty_line_items(self, app, make_user):
        user = make_user(username="empty_inv", email="empty_inv@example.com")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert invoice["line_items"] == []

    def test_totals_are_zero(self, app, make_user):
        user = make_user(username="empty_tot", email="empty_tot@example.com")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        totals = invoice["totals"]
        assert totals["record_count"] == 0
        assert totals["prompt_tokens"] == 0
        assert totals["completion_tokens"] == 0
        assert totals["total_tokens"] == 0
        assert totals["cost_estimate"] is None

    def test_draft_flag_is_true(self, app, make_user):
        user = make_user(username="draft_flag", email="draft_flag@example.com")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert invoice["draft"] is True

    def test_period_and_scope_echo(self, app, make_user):
        user = make_user(username="period_echo", email="period_echo@example.com")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert invoice["scope"] == "user"
        assert invoice["scope_id"] == user.id
        assert invoice["period"] == "2026-10"


class TestGenerateInvoiceUserScope:
    """Invoice aggregation for a user scope."""

    def test_single_record_produces_one_line_item(self, app, make_user):
        user = make_user(username="single_li", email="single_li@example.com")
        _add_record(user_id=user.id, period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert len(invoice["line_items"]) == 1

    def test_line_item_fields_are_accurate(self, app, make_user):
        user = make_user(username="li_acc", email="li_acc@example.com")
        _add_record(
            user_id=user.id,
            provider="anthropic",
            model="claude-3-5-haiku-latest",
            prompt_tokens=200,
            completion_tokens=80,
            cost_estimate=0.005,
            period="2026-10",
        )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        item = invoice["line_items"][0]
        assert item["provider"] == "anthropic"
        assert item["model"] == "claude-3-5-haiku-latest"
        assert item["prompt_tokens"] == 200
        assert item["completion_tokens"] == 80
        assert item["total_tokens"] == 280
        assert item["cost_estimate"] == 0.005
        assert item["cost_is_estimate"] is True
        assert item["record_count"] == 1

    def test_multiple_records_same_provider_model_aggregated(self, app, make_user):
        user = make_user(username="multi_agg", email="multi_agg@example.com")
        for _ in range(3):
            _add_record(
                user_id=user.id,
                provider="openai",
                model="gpt-4o",
                prompt_tokens=100,
                completion_tokens=50,
                cost_estimate=0.001,
                period="2026-10",
            )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert len(invoice["line_items"]) == 1
        item = invoice["line_items"][0]
        assert item["record_count"] == 3
        assert item["prompt_tokens"] == 300
        assert item["completion_tokens"] == 150
        assert item["total_tokens"] == 450
        assert item["cost_estimate"] == pytest.approx(0.003, abs=1e-6)

    def test_multiple_provider_model_pairs_produce_multiple_line_items(self, app, make_user):
        user = make_user(username="multi_li", email="multi_li@example.com")
        _add_record(user_id=user.id, provider="openai", model="gpt-4o", period="2026-10")
        _add_record(
            user_id=user.id,
            provider="anthropic",
            model="claude-3-5-haiku-latest",
            period="2026-10",
        )
        _add_record(
            user_id=user.id,
            provider="openai",
            model="gpt-4o-mini",
            period="2026-10",
        )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert len(invoice["line_items"]) == 3
        providers = {item["provider"] for item in invoice["line_items"]}
        assert "openai" in providers
        assert "anthropic" in providers

    def test_totals_match_sum_of_line_items(self, app, make_user):
        user = make_user(username="totals_sum", email="totals_sum@example.com")
        _add_record(
            user_id=user.id,
            provider="openai",
            model="gpt-4o",
            prompt_tokens=100,
            completion_tokens=50,
            cost_estimate=0.002,
            period="2026-10",
        )
        _add_record(
            user_id=user.id,
            provider="anthropic",
            model="claude-3-5-haiku-latest",
            prompt_tokens=200,
            completion_tokens=80,
            cost_estimate=0.003,
            period="2026-10",
        )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        totals = invoice["totals"]
        assert totals["record_count"] == 2
        assert totals["prompt_tokens"] == 300
        assert totals["completion_tokens"] == 130
        assert totals["total_tokens"] == 430
        assert totals["cost_estimate"] == pytest.approx(0.005, abs=1e-6)
        assert totals["cost_is_estimate"] is True

    def test_rows_outside_period_are_excluded(self, app, make_user):
        user = make_user(username="period_iso", email="period_iso@example.com")
        _add_record(user_id=user.id, period="2026-10")
        _add_record(user_id=user.id, period="2026-09")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert invoice["totals"]["record_count"] == 1

    def test_records_for_other_users_are_excluded(self, app, make_user):
        user1 = make_user(username="u1_excl", email="u1_excl@example.com")
        user2 = make_user(username="u2_excl", email="u2_excl@example.com")
        _add_record(user_id=user1.id, period="2026-10")
        _add_record(user_id=user2.id, period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user1.id, period="2026-10")
        assert invoice["totals"]["record_count"] == 1


class TestGenerateInvoiceWorkspaceScope:
    """Invoice aggregation for a workspace scope."""

    def test_workspace_invoice_aggregates_rows_for_that_workspace(self, app, make_user):
        user = make_user(username="ws_inv", email="ws_inv@example.com")
        ws = _make_workspace(user_id=user.id)
        _add_record(user_id=user.id, workspace_id=ws.id, period="2026-10")
        invoice = generate_invoice(scope="workspace", scope_id=ws.id, period="2026-10")
        assert invoice["scope"] == "workspace"
        assert invoice["scope_id"] == ws.id
        assert invoice["totals"]["record_count"] == 1

    def test_workspace_invoice_excludes_other_workspace_rows(self, app, make_user):
        user = make_user(username="ws_excl", email="ws_excl@example.com")
        ws1 = _make_workspace(user_id=user.id, name="WS1")
        ws2 = _make_workspace(user_id=user.id, name="WS2")
        _add_record(user_id=user.id, workspace_id=ws1.id, period="2026-10")
        _add_record(user_id=user.id, workspace_id=ws2.id, period="2026-10")
        invoice = generate_invoice(scope="workspace", scope_id=ws1.id, period="2026-10")
        assert invoice["totals"]["record_count"] == 1

    def test_workspace_invoice_empty_period(self, app, make_user):
        user = make_user(username="ws_empty", email="ws_empty@example.com")
        ws = _make_workspace(user_id=user.id)
        invoice = generate_invoice(scope="workspace", scope_id=ws.id, period="2026-10")
        assert invoice["line_items"] == []
        assert invoice["totals"]["cost_estimate"] is None
        assert invoice["draft"] is True

    def test_multiple_workspaces_produce_independent_invoices(self, app, make_user):
        user = make_user(username="ws_multi", email="ws_multi@example.com")
        ws1 = _make_workspace(user_id=user.id, name="Alpha")
        ws2 = _make_workspace(user_id=user.id, name="Beta")
        for _ in range(2):
            _add_record(
                user_id=user.id,
                workspace_id=ws1.id,
                period="2026-10",
                prompt_tokens=100,
                completion_tokens=40,
                cost_estimate=0.001,
            )
        _add_record(
            user_id=user.id,
            workspace_id=ws2.id,
            period="2026-10",
            prompt_tokens=500,
            completion_tokens=200,
            cost_estimate=0.010,
        )
        inv1 = generate_invoice(scope="workspace", scope_id=ws1.id, period="2026-10")
        inv2 = generate_invoice(scope="workspace", scope_id=ws2.id, period="2026-10")
        assert inv1["totals"]["record_count"] == 2
        assert inv2["totals"]["record_count"] == 1
        assert inv2["totals"]["prompt_tokens"] == 500


class TestGenerateInvoiceNullLabels:
    """Records with NULL provider/model are labelled 'unknown'."""

    def test_null_provider_appears_as_unknown(self, app, make_user):
        user = make_user(username="null_prov", email="null_prov@example.com")
        _add_record(user_id=user.id, provider=None, model="gpt-4o", period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        item = invoice["line_items"][0]
        assert item["provider"] == "unknown"

    def test_null_model_appears_as_unknown(self, app, make_user):
        user = make_user(username="null_model", email="null_model@example.com")
        _add_record(user_id=user.id, provider="openai", model=None, period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        item = invoice["line_items"][0]
        assert item["model"] == "unknown"


class TestGenerateInvoiceCostNone:
    """Records without a cost estimate are handled gracefully."""

    def test_no_cost_rows_produce_none_total(self, app, make_user):
        user = make_user(username="no_cost", email="no_cost@example.com")
        _add_record(user_id=user.id, cost_estimate=None, period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert invoice["totals"]["cost_estimate"] is None
        assert invoice["line_items"][0]["cost_estimate"] is None

    def test_mixed_cost_rows_exclude_none_from_total(self, app, make_user):
        user = make_user(username="mixed_cost", email="mixed_cost@example.com")
        _add_record(
            user_id=user.id,
            provider="openai",
            model="gpt-4o",
            cost_estimate=0.002,
            period="2026-10",
        )
        _add_record(
            user_id=user.id,
            provider="unknown_provider",
            model="mystery",
            cost_estimate=None,
            period="2026-10",
        )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        assert invoice["totals"]["cost_estimate"] == pytest.approx(0.002, abs=1e-6)


class TestGenerateInvoiceValidation:
    """Input validation in generate_invoice."""

    @pytest.mark.parametrize(
        "bad_period",
        ["2026-13", "2026-00", "2026", "abc", "", "2026/10"],
    )
    def test_bad_period_raises_invoice_error(self, app, make_user, bad_period):
        user = make_user(username="bad_period", email="bad_period@example.com")
        with pytest.raises(InvoiceError) as exc_info:
            generate_invoice(scope="user", scope_id=user.id, period=bad_period)
        assert exc_info.value.kind == "period"

    def test_date_string_is_truncated_to_month(self, app, make_user):
        user = make_user(username="date_trunc", email="date_trunc@example.com")
        _add_record(user_id=user.id, period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10-15")
        assert invoice["period"] == "2026-10"
        assert invoice["totals"]["record_count"] == 1

    def test_bad_scope_raises_invoice_error(self, app, make_user):
        user = make_user(username="bad_scope", email="bad_scope@example.com")
        with pytest.raises(InvoiceError) as exc_info:
            generate_invoice(scope="organisation", scope_id=user.id, period="2026-10")
        assert exc_info.value.kind == "scope"

    def test_invalid_scope_id_raises_invoice_error(self, app):
        with pytest.raises(InvoiceError) as exc_info:
            generate_invoice(scope="user", scope_id="not-an-int", period="2026-10")
        assert exc_info.value.kind == "invalid"


# ---------------------------------------------------------------------------
# API route tests
# ---------------------------------------------------------------------------


class TestApiListInvoicesRoute:
    """GET /billing/api/invoices?period=YYYY-MM"""

    def _login_user(self, client, make_user):
        user = make_user(username="api_inv", email="api_inv@example.com")
        client.post(
            "/auth/login",
            data={"email": "api_inv@example.com", "password": "supersecret123"},
            follow_redirects=True,
        )
        return user

    def test_requires_authentication(self, client, app):
        resp = client.get("/billing/api/invoices?period=2026-10")
        assert resp.status_code in (302, 401)

    def test_missing_period_returns_400(self, client, app, make_user):
        self._login_user(client, make_user)
        resp = client.get("/billing/api/invoices")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["kind"] == "invalid"

    def test_invalid_period_returns_400(self, client, app, make_user):
        self._login_user(client, make_user)
        resp = client.get("/billing/api/invoices?period=bad-period")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["kind"] == "period"

    def test_returns_draft_invoice_for_user(self, client, app, make_user):
        user = self._login_user(client, make_user)
        _add_record(user_id=user.id, period="2026-10")
        resp = client.get("/billing/api/invoices?period=2026-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["draft"] is True
        assert data["scope"] == "user"
        assert data["scope_id"] == user.id
        assert data["period"] == "2026-10"
        assert data["totals"]["record_count"] == 1
        assert len(data["line_items"]) == 1

    def test_empty_period_returns_zero_totals(self, client, app, make_user):
        self._login_user(client, make_user)
        resp = client.get("/billing/api/invoices?period=2026-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["line_items"] == []
        assert data["totals"]["record_count"] == 0
        assert data["totals"]["cost_estimate"] is None

    def test_workspace_scope_with_access(self, client, app, make_user):
        user = self._login_user(client, make_user)
        ws = _make_workspace(user_id=user.id)
        _add_record(user_id=user.id, workspace_id=ws.id, period="2026-10")
        resp = client.get(f"/billing/api/invoices?period=2026-10&workspace_id={ws.id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scope"] == "workspace"
        assert data["scope_id"] == ws.id
        assert data["totals"]["record_count"] == 1

    def test_workspace_scope_no_access_returns_404(self, client, app, make_user):
        self._login_user(client, make_user)
        other_user = make_user(username="other_api", email="other_api@example.com")
        other_ws = _make_workspace(user_id=other_user.id, name="Other WS")
        resp = client.get(f"/billing/api/invoices?period=2026-10&workspace_id={other_ws.id}")
        assert resp.status_code == 404


class TestApiGetInvoiceRoute:
    """GET /billing/api/invoices/<period>"""

    def _login_user(self, client, make_user, suffix="rt"):
        username = f"get_inv_{suffix}"
        email = f"get_inv_{suffix}@example.com"
        user = make_user(username=username, email=email)
        client.post(
            "/auth/login",
            data={"email": email, "password": "supersecret123"},
            follow_redirects=True,
        )
        return user

    def test_requires_authentication(self, client, app):
        resp = client.get("/billing/api/invoices/2026-10")
        assert resp.status_code in (302, 401)

    def test_returns_draft_invoice(self, client, app, make_user):
        user = self._login_user(client, make_user, suffix="basic")
        _add_record(user_id=user.id, period="2026-10")
        resp = client.get("/billing/api/invoices/2026-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["draft"] is True
        assert data["period"] == "2026-10"
        assert data["totals"]["record_count"] == 1

    def test_invalid_period_in_path_returns_400(self, client, app, make_user):
        self._login_user(client, make_user, suffix="badp")
        resp = client.get("/billing/api/invoices/not-a-period")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["kind"] == "period"

    def test_workspace_scope_with_access(self, client, app, make_user):
        user = self._login_user(client, make_user, suffix="ws")
        ws = _make_workspace(user_id=user.id, name="RT WS")
        _add_record(user_id=user.id, workspace_id=ws.id, period="2026-10")
        resp = client.get(f"/billing/api/invoices/2026-10?workspace_id={ws.id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["scope"] == "workspace"
        assert data["totals"]["record_count"] == 1

    def test_workspace_scope_no_access_returns_404(self, client, app, make_user):
        self._login_user(client, make_user, suffix="noacc")
        other_user = make_user(username="other_rt", email="other_rt@example.com")
        other_ws = _make_workspace(user_id=other_user.id, name="Other RT WS")
        resp = client.get(f"/billing/api/invoices/2026-10?workspace_id={other_ws.id}")
        assert resp.status_code == 404

    def test_empty_period_via_path_returns_zero_totals(self, client, app, make_user):
        self._login_user(client, make_user, suffix="empty")
        resp = client.get("/billing/api/invoices/2026-10")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["line_items"] == []
        assert data["totals"]["cost_estimate"] is None


class TestLineItemAccuracy:
    """Detailed accuracy checks for multi-record, multi-dimension scenarios."""

    def test_cost_accumulates_correctly_across_records(self, app, make_user):
        user = make_user(username="cost_acc", email="cost_acc@example.com")
        costs = [0.001, 0.002, 0.0005]
        for cost in costs:
            _add_record(
                user_id=user.id,
                provider="openai",
                model="gpt-4o",
                prompt_tokens=10,
                completion_tokens=5,
                cost_estimate=cost,
                period="2026-10",
            )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        item = invoice["line_items"][0]
        assert item["cost_estimate"] == pytest.approx(sum(costs), abs=1e-6)
        assert item["record_count"] == 3

    def test_line_items_sorted_by_provider_then_model(self, app, make_user):
        user = make_user(username="sorted_li", email="sorted_li@example.com")
        pairs = [
            ("openai", "gpt-4o"),
            ("anthropic", "claude-3-5-haiku-latest"),
            ("openai", "gpt-4o-mini"),
        ]
        for provider, model in pairs:
            _add_record(user_id=user.id, provider=provider, model=model, period="2026-10")
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        keys = [(item["provider"], item["model"]) for item in invoice["line_items"]]
        assert keys == sorted(keys)

    def test_tokens_aggregated_correctly(self, app, make_user):
        user = make_user(username="tokens_agg", email="tokens_agg@example.com")
        rows = [
            {"prompt_tokens": 100, "completion_tokens": 40},
            {"prompt_tokens": 200, "completion_tokens": 60},
            {"prompt_tokens": 50, "completion_tokens": 20},
        ]
        for row in rows:
            _add_record(
                user_id=user.id,
                provider="openai",
                model="gpt-4o",
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                period="2026-10",
            )
        invoice = generate_invoice(scope="user", scope_id=user.id, period="2026-10")
        item = invoice["line_items"][0]
        assert item["prompt_tokens"] == 350
        assert item["completion_tokens"] == 120
        assert item["total_tokens"] == 470
