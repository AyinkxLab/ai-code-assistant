"""add named project chat sessions

Revision ID: f7b8c9d0e1f2
Revises: e6f7a8b9c0d1
"""

from alembic import op
import sqlalchemy as sa


revision = "f7b8c9d0e1f2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "project_chat_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("project_chat_sessions", schema=None) as batch_op:
        batch_op.create_index("ix_project_chat_sessions_project_id", ["project_id"], unique=False)

    op.add_column(
        "project_messages",
        sa.Column("session_id", sa.Integer(), nullable=True),
    )
    with op.batch_alter_table("project_messages", schema=None) as batch_op:
        batch_op.create_index("ix_project_messages_session_id", ["session_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_project_messages_session_id",
            "project_chat_sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )

    connection = op.get_bind()
    now = sa.func.current_timestamp()
    project_ids = connection.execute(sa.text("SELECT DISTINCT project_id FROM project_messages"))
    for (project_id,) in project_ids:
        connection.execute(
            sa.text(
                "INSERT INTO project_chat_sessions "
                "(project_id, title, created_at, updated_at) "
                "VALUES (:project_id, 'General', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id},
        )
        session_id = connection.execute(
            sa.text(
                "SELECT id FROM project_chat_sessions "
                "WHERE project_id = :project_id ORDER BY id DESC LIMIT 1"
            ),
            {"project_id": project_id},
        ).scalar_one()
        connection.execute(
            sa.text(
                "UPDATE project_messages SET session_id = :session_id "
                "WHERE project_id = :project_id AND session_id IS NULL"
            ),
            {"project_id": project_id, "session_id": session_id},
        )
    with op.batch_alter_table("project_messages", schema=None) as batch_op:
        batch_op.alter_column("session_id", existing_type=sa.Integer(), nullable=False)


def downgrade():
    with op.batch_alter_table("project_messages", schema=None) as batch_op:
        batch_op.drop_constraint("fk_project_messages_session_id", type_="foreignkey")
        batch_op.drop_index("ix_project_messages_session_id")
        batch_op.drop_column("session_id")
    with op.batch_alter_table("project_chat_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_project_chat_sessions_project_id")
    op.drop_table("project_chat_sessions")