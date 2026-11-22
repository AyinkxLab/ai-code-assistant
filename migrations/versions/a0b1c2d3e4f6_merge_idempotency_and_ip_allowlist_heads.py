"""Merge the idempotency-key and IP-allowlist branches.

Both ``b1c2d3e4f5a6`` (POST idempotency keys) and ``f2a1b0c9d8e7`` (workspace
IP allowlists) landed on ``main`` as heads. A single descendant keeps
``flask db upgrade`` reachable from one head.
"""

revision = "a0b1c2d3e4f6"
down_revision = ("b1c2d3e4f5a6", "f2a1b0c9d8e7")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
