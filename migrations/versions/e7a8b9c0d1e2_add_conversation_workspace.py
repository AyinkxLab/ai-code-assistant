"""add workspace scope to conversations

Revision ID: e7a8b9c0d1e2
Revises: d8e9f0a1b2c3
Create Date: 2026-09-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7a8b9c0d1e2'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('workspace_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_conversations_workspace_id'), ['workspace_id'], unique=False
        )
        # ON DELETE SET NULL: removing a workspace unfiles its conversations
        # (they fall back to the unscoped list) instead of destroying history.
        batch_op.create_foreign_key(
            'fk_conversations_workspace_id',
            'workspaces',
            ['workspace_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('conversations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_conversations_workspace_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_conversations_workspace_id'))
        batch_op.drop_column('workspace_id')
