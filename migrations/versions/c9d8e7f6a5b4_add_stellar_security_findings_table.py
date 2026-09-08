"""add stellar security findings table

Revision ID: c9d8e7f6a5b4
Revises: a7f8b9c0d1e2
Create Date: 2026-09-08 00:00:00.000000

Adds the per-project ``stellar_security_findings`` table that persists the
structured findings produced by the ``stellar_security`` AI analysis
(severity, category, confidence, file + line, evidence, explanation,
remediation). Findings belong to a single project and are read owner-scoped.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c9d8e7f6a5b4"
down_revision = "a7f8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stellar_security_findings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("file", sa.String(length=2000), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.String(length=20), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("stellar_security_findings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_stellar_security_findings_project_id"),
            ["project_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("stellar_security_findings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_stellar_security_findings_project_id"))

    op.drop_table("stellar_security_findings")
