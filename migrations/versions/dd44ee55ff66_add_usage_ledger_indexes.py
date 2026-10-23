"""add composite indexes for usage ledger aggregation

Revision ID: dd44ee55ff66
Revises: aa11bb22cc33
Create Date: 2026-10-23 00:00:00.000000

Phase 10 (#436): the ledger is read with a bounded time window plus an optional
scope filter, then grouped by source/provider/model and the scope columns. Add
composite indexes so the filter and grouping columns are served by one index
instead of a full scan. Existing single-column indexes are kept for callers that
filter on one column without a window.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "dd44ee55ff66"
down_revision = "aa11bb22cc33"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_usage_records_user_created", ["user_id", "created_at"]),
    ("ix_usage_records_workspace_created", ["workspace_id", "created_at"]),
    ("ix_usage_records_project_created", ["project_id", "created_at"]),
    ("ix_usage_records_source_created", ["source", "created_at"]),
    (
        "ix_usage_records_provider_model_created",
        ["provider", "model", "created_at"],
    ),
    ("ix_usage_records_status_created", ["status", "created_at"]),
)


def upgrade():
    for name, columns in _INDEXES:
        op.create_index(name, "usage_records", columns)


def downgrade():
    for name, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name="usage_records")
