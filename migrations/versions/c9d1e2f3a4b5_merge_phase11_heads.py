"""Merge the SIEM and webhook-secret branches before domain verification.

Both ``e8f9a0b1c2d3`` (SIEM endpoints) and ``f4a5b6c7d8e9`` (webhook secret
rotation) landed on ``main`` as heads. A single descendant keeps
``flask db upgrade`` reachable from one head.
"""

revision = "c9d1e2f3a4b5"
down_revision = ("e8f9a0b1c2d3", "f4a5b6c7d8e9")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
