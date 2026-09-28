"""add workspace slugs

Adds a stored, normalized ``slug`` to ``workspaces`` and makes it unique per
user. Existing rows are backfilled from their current name; rows that would
collide with an already-assigned slug (pre-existing duplicate names, which the
old schema allowed) get a ``-2``/``-3``/... suffix so the unique constraint can
be created without losing data.

Revision ID: c4d5e6f7a8b9
Revises: d8e9f0a1b2c3
Create Date: 2026-09-28 12:00:00.000000

"""
import re

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'd8e9f0a1b2c3'
branch_labels = None
depends_on = None


SLUG_MAX_LENGTH = 200
SLUG_FALLBACK = "workspace"
_DASHES_RE = re.compile(r"-{2,}")


def _slugify(name):
    """Mirror app.models.workspace.slugify_workspace_name for the backfill."""
    normalized = "".join(ch if ch.isalnum() else "-" for ch in (name or "").lower())
    slug = _DASHES_RE.sub("-", normalized).strip("-")
    slug = slug[:SLUG_MAX_LENGTH].strip("-")
    return slug or SLUG_FALLBACK


def _dedupe(base, taken):
    """Return ``base``, or ``base-2``/``base-3``/... if ``base`` is already used.

    Suffixing can push a slug past the column width, so re-check the truncated
    value and keep counting rather than emitting a duplicate.
    """
    if base not in taken:
        return base
    counter = 2
    while True:
        candidate = f"{base}-{counter}"[:SLUG_MAX_LENGTH]
        if candidate not in taken:
            return candidate
        counter += 1


def upgrade():
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('slug', sa.String(length=SLUG_MAX_LENGTH), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, user_id, name FROM workspaces ORDER BY id")
    ).fetchall()

    # Track taken slugs per user so the backfill mirrors the per-user
    # uniqueness the constraint will enforce.
    taken_by_user = {}
    for row_id, user_id, name in rows:
        taken = taken_by_user.setdefault(user_id, set())
        slug = _dedupe(_slugify(name), taken)
        taken.add(slug)
        connection.execute(
            sa.text("UPDATE workspaces SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": row_id},
        )

    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.alter_column(
            'slug', existing_type=sa.String(length=SLUG_MAX_LENGTH), nullable=False
        )
        batch_op.create_unique_constraint('uq_workspaces_user_slug', ['user_id', 'slug'])
        batch_op.create_index(batch_op.f('ix_workspaces_slug'), ['slug'], unique=False)


def downgrade():
    with op.batch_alter_table('workspaces', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workspaces_slug'))
        batch_op.drop_constraint('uq_workspaces_user_slug', type_='unique')
        batch_op.drop_column('slug')