"""widen API token scopes for capability scopes

Revision ID: a6b7c8d9e0f1
Revises: b4e2f1a3c5d7
Create Date: 2026-11-25 00:00:00.000000

Phase 11 (#603): API tokens may carry a least-privilege subset of the owner's
capabilities in addition to the legacy read/write/admin ranks. Existing rows keep
their meaning; the column is widened so the canonical capability list fits.
"""

import sqlalchemy as sa
from alembic import op

revision = "a6b7c8d9e0f1"
down_revision = "b4e2f1a3c5d7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.alter_column(
            "scopes",
            existing_type=sa.String(length=100),
            type_=sa.String(length=512),
            existing_nullable=False,
        )


def downgrade():
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.alter_column(
            "scopes",
            existing_type=sa.String(length=512),
            type_=sa.String(length=100),
            existing_nullable=False,
        )
