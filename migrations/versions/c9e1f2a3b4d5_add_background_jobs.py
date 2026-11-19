"""add durable background jobs

Revision ID: c9e1f2a3b4d5
Revises: a1c2e3f4b5d6
Create Date: 2026-11-19 00:00:00.000000

Phase 12: persisted work queue for long-running exports, scans, and webhook
deliveries (issue #699). Jobs survive a process restart and are worked by
``flask jobs worker``.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c9e1f2a3b4d5"
down_revision = "a1c2e3f4b5d6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.String(length=2000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_jobs_status_available", "jobs", ["status", "available_at"])
    op.create_index("ix_jobs_kind_status", "jobs", ["kind", "status"])


def downgrade():
    op.drop_index("ix_jobs_kind_status", table_name="jobs")
    op.drop_index("ix_jobs_status_available", table_name="jobs")
    op.drop_table("jobs")
