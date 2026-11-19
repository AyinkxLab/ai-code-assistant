"""add composite indexes for hot read paths

Revision ID: a1c2e3f4b5d6
Revises: b8d0f2a4c6e8
Create Date: 2026-11-19 00:00:00.000000

Phase 12: indexes for the filters and orderings the application already uses
(issue #697). Every index is paired with the query it serves in
``docs/database-indexes.md``. Single-column indexes stay in place; these
composites replace the extra sort/filter step on the hot paths.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c2e3f4b5d6"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_conversations_user_updated", "conversations", ["user_id", "updated_at"]),
    ("ix_conversations_workspace_updated", "conversations", ["workspace_id", "updated_at"]),
    ("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]),
    ("ix_project_messages_session_created", "project_messages", ["session_id", "created_at"]),
    ("ix_audit_logs_user_created", "audit_logs", ["user_id", "created_at"]),
    ("ix_audit_logs_action_created", "audit_logs", ["action", "created_at"]),
)


def upgrade():
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
