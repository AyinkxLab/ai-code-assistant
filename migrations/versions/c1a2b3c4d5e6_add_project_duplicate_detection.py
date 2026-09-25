"""add project duplicate-import detection (issue #87)

Revision ID: c1a2b3c4d5e6
Revises: a54b1c2d3e4f
Create Date: 2026-09-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a2b3c4d5e6'
down_revision = 'a54b1c2d3e4f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('default_branch', sa.String(length=255), nullable=True))
        batch_op.create_index(
            batch_op.f('ix_projects_content_hash'), ['content_hash'], unique=False
        )


def downgrade():
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_projects_content_hash'))
        batch_op.drop_column('default_branch')
        batch_op.drop_column('content_hash')
