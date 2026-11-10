"""milestones table and audit_logs.phase

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("phase", sa.String(16)))
    op.create_index("ix_audit_logs_run_id_phase", "audit_logs", ["run_id", "phase"])

    op.create_table(
        "milestones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("contractor_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(512), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acceptance_criteria", JSON_TYPE, nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("terms_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("verification_status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("payment_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_milestones"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], name="fk_milestones_payment_id_payments"),
        sa.UniqueConstraint("run_id", "project_id", "sequence", name="uq_milestones_run_id"),
    )
    op.create_index("ix_milestones_run_id", "milestones", ["run_id"])


def downgrade() -> None:
    op.drop_table("milestones")
    op.drop_index("ix_audit_logs_run_id_phase", table_name="audit_logs")
    op.drop_column("audit_logs", "phase")
