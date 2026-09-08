"""add per-user stellar network selection

Revision ID: a7f8b9c0d1e2
Revises: b3c2d1a0f9e8
Create Date: 2026-09-08 00:00:00.000000

Adds an optional ``stellar_network`` column to ``users`` for the read-only
Stellar network switcher. ``NULL`` keeps the operator-configured
``STELLAR_NETWORK`` default authoritative; only the fixed supported networks
(mainnet, testnet, futurenet, custom/local) are ever stored.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a7f8b9c0d1e2"
down_revision = "b3c2d1a0f9e8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("stellar_network", sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("stellar_network")
