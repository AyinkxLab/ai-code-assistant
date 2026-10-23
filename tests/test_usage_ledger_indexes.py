"""Query-shape guards for the usage ledger indexes (#436).

The ledger is read with a bounded time window plus an optional scope filter and
then grouped by a small set of columns. These tests pin the model's composite
indexes and the plans SQLite chooses for those shapes.

Expected plans (SQLite, in-memory test database):

- user/window + group by source:
  ``SEARCH usage_records USING INDEX ix_usage_records_user_created (...)``
- workspace/window + group by provider:
  ``... ix_usage_records_workspace_created ...``
- source/window + group by model:
  ``... ix_usage_records_source_created ...``
- status/window: covering ``ix_usage_records_status_created``
- provider+model/window: covering ``ix_usage_records_provider_model_created``

SQLite still builds a temporary b-tree for grouped columns it cannot read from
the index; that work is bounded by the rows the index filter returns.
"""

from sqlalchemy import text

from app.models import UsageRecord

INDEXES = {
    "ix_usage_records_user_created": ("user_id", "created_at"),
    "ix_usage_records_workspace_created": ("workspace_id", "created_at"),
    "ix_usage_records_project_created": ("project_id", "created_at"),
    "ix_usage_records_source_created": ("source", "created_at"),
    "ix_usage_records_provider_model_created": ("provider", "model", "created_at"),
    "ix_usage_records_status_created": ("status", "created_at"),
}


def _plan(db, query: str) -> str:
    rows = db.session.execute(text("EXPLAIN QUERY PLAN " + query)).all()
    return " | ".join(str(row[3]) for row in rows)


def test_composite_indexes_are_declared():
    declared = {
        index.name: tuple(column.name for column in index.columns)
        for index in UsageRecord.__table__.indexes
    }
    for name, columns in INDEXES.items():
        assert declared.get(name) == columns, name


def test_user_window_aggregate_uses_composite_index(db):
    plan = _plan(
        db,
        "SELECT source, COUNT(id), SUM(total_tokens) FROM usage_records "
        "WHERE user_id = 1 AND created_at >= '2026-01-01' GROUP BY source",
    )
    assert "ix_usage_records_user_created" in plan, plan
    assert "SCAN usage_records" not in plan, plan


def test_workspace_window_aggregate_uses_composite_index(db):
    plan = _plan(
        db,
        "SELECT provider, COUNT(id), SUM(cost_estimate) FROM usage_records "
        "WHERE workspace_id = 1 AND created_at >= '2026-01-01' GROUP BY provider",
    )
    assert "ix_usage_records_workspace_created" in plan, plan


def test_source_window_aggregate_uses_composite_index(db):
    plan = _plan(
        db,
        "SELECT model, COUNT(id), SUM(total_tokens) FROM usage_records "
        "WHERE source = 'chat' AND created_at >= '2026-01-01' GROUP BY model",
    )
    assert "ix_usage_records_source_created" in plan, plan


def test_status_window_aggregate_uses_covering_index(db):
    plan = _plan(
        db,
        "SELECT status, COUNT(id) FROM usage_records "
        "WHERE status = 'error' AND created_at >= '2026-01-01' GROUP BY status",
    )
    assert "COVERING INDEX ix_usage_records_status_created" in plan, plan


def test_provider_model_window_aggregate_uses_covering_index(db):
    plan = _plan(
        db,
        "SELECT model, COUNT(id) FROM usage_records "
        "WHERE provider = 'openai' AND model = 'gpt-x' "
        "AND created_at >= '2026-01-01' GROUP BY model",
    )
    assert "COVERING INDEX ix_usage_records_provider_model_created" in plan, plan


def test_project_window_filter_uses_composite_index(db):
    plan = _plan(
        db,
        "SELECT COUNT(id), SUM(total_tokens) FROM usage_records "
        "WHERE project_id = 1 AND created_at >= '2026-01-01'",
    )
    assert "ix_usage_records_project_created" in plan, plan
