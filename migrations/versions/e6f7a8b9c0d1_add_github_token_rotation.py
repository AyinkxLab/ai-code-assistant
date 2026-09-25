"""add github token rotation fields

Revision ID: e6f7a8b9c0d1
Revises: d4e5f6a7b8c9
"""
from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("github_accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("refresh_token_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("github_accounts", schema=None) as batch_op:
        batch_op.drop_column("token_expires_at")
        batch_op.drop_column("refresh_token_encrypted")