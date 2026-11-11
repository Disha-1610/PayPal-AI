"""runs index

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("goal", sa.String(2000), nullable=False),
        sa.Column("max_spend", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("deliver_by", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_runs"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_runs_user_id"),
    )
    op.create_index("ix_runs_user_id_created_at", "runs", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_user_id_created_at", table_name="runs")
    op.drop_table("runs")
