"""add project indexing progress

Revision ID: d4e5f6a7b8c9
Revises: b4c3d2e1f0a9
Create Date: 2026-09-23 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = 'b4c3d2e1f0a9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('progress', sa.Integer(), nullable=False, server_default='0')
        )


def downgrade():
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('progress')
