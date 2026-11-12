"""run counterparties

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_counterparties",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("contact", sa.JSON(), nullable=False),
        sa.Column("email", sa.String(254)),
        sa.Column("phone", sa.String(24)),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("awaiting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reply_deadline", sa.DateTime(timezone=True)),
        sa.Column("last_offer", sa.JSON()),
        sa.Column("reminder_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_contacted_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_run_counterparties_deadline", "run_counterparties", ["awaiting", "reply_deadline"])
    op.create_index("ix_run_counterparties_phone", "run_counterparties", ["phone"])
    op.create_index("ix_run_counterparties_email", "run_counterparties", ["email"])


def downgrade() -> None:
    op.drop_index("ix_run_counterparties_email", "run_counterparties")
    op.drop_index("ix_run_counterparties_phone", "run_counterparties")
    op.drop_index("ix_run_counterparties_deadline", "run_counterparties")
    op.drop_table("run_counterparties")
