"""add audit hash chain

Revision ID: a1b2c3d4e5f7
Revises: c8f1d2e3a4b5
Create Date: 2026-11-22 00:00:00.000000

Phase 11 (#606): scope audit entries to a workspace and store the previous and
record hashes for tamper-evident verification. Existing rows keep ``NULL``
hashes (reported as unhashed by ``flask audit verify``).
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f7"
down_revision = "c8f1d2e3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("prev_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("record_hash", sa.String(length=64), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_audit_logs_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_audit_logs_workspace_id",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade():
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_audit_logs_workspace_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_audit_logs_workspace_id"))
        batch_op.drop_column("record_hash")
        batch_op.drop_column("prev_hash")
        batch_op.drop_column("workspace_id")
