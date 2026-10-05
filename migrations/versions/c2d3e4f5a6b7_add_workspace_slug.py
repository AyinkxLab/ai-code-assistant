"""add workspace slug

Revision ID: c2d3e4f5a6b7
Revises: b2c3d4e5f6a7
Create Date: 2026-10-05 11:40:00.000000

"""
from alembic import op
import re
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2d3e4f5a6b7'
down_revision = '91a2b3c4d5e8'
branch_labels = None
depends_on = None


def _generate_slug(name):
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-") or "workspace"


def upgrade():
    # 1. Add the column as nullable first so we can backfill.
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('slug', sa.String(200), nullable=True)
        )

    # 2. Backfill slug from name for every existing row.
    conn = op.get_bind()
    workspaces = conn.execute(sa.text("SELECT id, user_id, name FROM workspaces")).fetchall()

    # Track slugs per user to resolve collisions during backfill.
    slugs_by_user = {}
    for ws_id, user_id, name in workspaces:
        slug = _generate_slug(name)
        seen = slugs_by_user.setdefault(user_id, set())
        # Append a numeric suffix when two existing rows collide.
        candidate = slug
        counter = 2
        while candidate in seen:
            candidate = f"{slug}-{counter}"
            counter += 1
        seen.add(candidate)
        conn.execute(
            sa.text("UPDATE workspaces SET slug = :slug WHERE id = :id"),
            {"slug": candidate, "id": ws_id},
        )

    # 3. Set NOT NULL and add the unique index.
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.alter_column('slug', nullable=False)
        batch_op.create_index('ix_workspaces_slug', ['slug'])
        batch_op.create_unique_constraint('uq_workspace_user_slug', ['user_id', 'slug'])


def downgrade():
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_constraint('uq_workspace_user_slug', type_='unique')
        batch_op.drop_index('ix_workspaces_slug')
        batch_op.drop_column('slug')
