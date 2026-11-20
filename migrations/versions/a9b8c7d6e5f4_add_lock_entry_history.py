"""add lock entry history for plugin rollback

Revision ID: a9b8c7d6e5f4
Revises: e3f4a5b6c7d8
Create Date: 2026-11-20

Retains the previously resolved artifact (version, source, integrity, manifest)
so ``rollback_plugin`` can restore it even when the catalog no longer lists that
version (#363).
"""

import sqlalchemy as sa
from alembic import op

revision = "a9b8c7d6e5f4"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("plugin_lock_entries") as batch_op:
        batch_op.add_column(sa.Column("manifest", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("previous_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("previous_source", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("previous_integrity", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("previous_manifest", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("plugin_lock_entries") as batch_op:
        batch_op.drop_column("previous_manifest")
        batch_op.drop_column("previous_integrity")
        batch_op.drop_column("previous_source")
        batch_op.drop_column("previous_version")
        batch_op.drop_column("manifest")
