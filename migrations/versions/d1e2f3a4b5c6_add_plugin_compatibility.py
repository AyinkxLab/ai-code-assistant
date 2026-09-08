"""add plugin compatibility column

Revision ID: d1e2f3a4b5c6
Revises: c9d8e7f6a5b4
Create Date: 2026-09-08 00:00:00.000000

Adds the ``plugins.compatibility`` column that stores each plugin's optional
PEP 440 application-version specifier (e.g. ``>=0.8.0``) so the app can enforce
and expose plugin/application compatibility. ``NULL`` means the plugin supports
any application version.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "c9d8e7f6a5b4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "plugins",
        sa.Column("compatibility", sa.String(length=256), nullable=True),
    )


def downgrade():
    op.drop_column("plugins", "compatibility")
