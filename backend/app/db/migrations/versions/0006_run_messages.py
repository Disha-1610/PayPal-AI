"""run_messages

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_messages",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("agent", sa.String(32)),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("client_id", sa.String(128)),
        sa.Column("is_question", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "id", name="pk_run_messages"),
    )


def downgrade() -> None:
    op.drop_table("run_messages")
