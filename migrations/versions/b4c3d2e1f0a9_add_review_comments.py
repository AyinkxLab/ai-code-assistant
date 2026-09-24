"""add inline review comments (issue #52)

Revision ID: b4c3d2e1f0a9
Revises: c3d4e5f6a7b8
Create Date: 2026-09-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4c3d2e1f0a9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('review_comments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('block_index', sa.Integer(), nullable=True),
    sa.Column('line_start', sa.Integer(), nullable=True),
    sa.Column('line_end', sa.Integer(), nullable=True),
    sa.Column('resolved', sa.Boolean(), nullable=False),
    sa.Column('resolved_by', sa.Integer(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['project_messages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_id'], ['review_comments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resolved_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('review_comments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_review_comments_author_id'), ['author_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_review_comments_message_id'), ['message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_review_comments_project_id'), ['project_id'], unique=False)


def downgrade():
    with op.batch_alter_table('review_comments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_review_comments_project_id'))
        batch_op.drop_index(batch_op.f('ix_review_comments_message_id'))
        batch_op.drop_index(batch_op.f('ix_review_comments_author_id'))

    op.drop_table('review_comments')
