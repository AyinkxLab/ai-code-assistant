"""Merge the conversation-share and review-comment migration heads."""

revision = "b4c6d7e8f9a0"
down_revision = ("d4e5f6a7b8c9", "b4c5d6e7f8a9")
branch_labels = None
depends_on = None


def upgrade():
    """Join the two feature branches; both heads already applied their schema changes."""


def downgrade():
    """Do not reverse either parent branch from a merge-only revision."""
