"""add integration credentials

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-11-20 00:00:00.000000

Phase 12: workspace-scoped third-party integration secrets. Only the
AES-256-GCM ciphertext is stored; the plaintext is never persisted (issue
#683).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "integration_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("integration", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "integration",
            "name",
            name="uq_integration_credentials_workspace_integration_name",
        ),
    )
    with op.batch_alter_table("integration_credentials", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_integration_credentials_workspace_id"),
            ["workspace_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_integration_credentials_integration"),
            ["integration"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("integration_credentials", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_integration_credentials_integration"))
        batch_op.drop_index(batch_op.f("ix_integration_credentials_workspace_id"))

    op.drop_table("integration_credentials")
