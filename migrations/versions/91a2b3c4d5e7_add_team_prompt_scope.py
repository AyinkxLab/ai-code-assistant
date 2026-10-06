"""Add workspace-scoped team prompt libraries.

Revision ID: 91a2b3c4d5e7
Revises: d8e9f0a1b2c3
"""

from alembic import op
import sqlalchemy as sa

revision = "91a2b3c4d5e7"
down_revision = "91a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("prompts") as batch_op:
        batch_op.add_column(sa.Column("is_team", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("workspace_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_prompts_workspace_id_workspaces", "workspaces", ["workspace_id"], ["id"], ondelete="CASCADE")
        batch_op.create_index("ix_prompts_is_team", ["is_team"], unique=False)
        batch_op.create_index("ix_prompts_workspace_id", ["workspace_id"], unique=False)


def downgrade():
    with op.batch_alter_table("prompts") as batch_op:
        batch_op.drop_index("ix_prompts_workspace_id")
        batch_op.drop_index("ix_prompts_is_team")
        batch_op.drop_constraint("fk_prompts_workspace_id_workspaces", type_="foreignkey")
        batch_op.drop_column("workspace_id")
        batch_op.drop_column("is_team")
