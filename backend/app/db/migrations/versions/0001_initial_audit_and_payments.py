"""initial audit log and payment ledger

Revision ID: 0001
Revises:
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
APPEND_ONLY_TABLES = ("audit_logs", "payment_events", "payment_reconciliations")


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(128)),
        sa.Column("input_hash", sa.String(64)),
        sa.Column("decision", sa.String(32)),
        sa.Column("reason", sa.String(512)),
        sa.Column("state_diff", JSON_TYPE),
        sa.Column("metadata_json", JSON_TYPE),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_index("ix_audit_logs_run_id_timestamp", "audit_logs", ["run_id", "timestamp"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("authorization_id", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("provider_order_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
    )
    op.create_index("ix_payments_run_id", "payments", ["run_id"])

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("provider_ref", sa.String(128)),
        sa.Column("payload", JSON_TYPE),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], name="fk_payment_events_payment_id_payments"),
        sa.PrimaryKeyConstraint("id", name="pk_payment_events"),
        sa.UniqueConstraint("payment_id", "request_id", "event_type", name="uq_payment_events_payment_id"),
    )
    op.create_index("ix_payment_events_payment_id_created_at", "payment_events", ["payment_id", "created_at"])

    op.create_table(
        "payment_reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("ledger_status", sa.String(16), nullable=False),
        sa.Column("provider_status", sa.String(32), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("detail", JSON_TYPE),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name="fk_payment_reconciliations_payment_id_payments"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_reconciliations"),
    )
    op.create_index("ix_payment_reconciliations_payment_id", "payment_reconciliations", ["payment_id"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% on % is not allowed: table is append-only', TG_OP, TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION forbid_mutation()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
        op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")
    op.drop_table("payment_reconciliations")
    op.drop_table("payment_events")
    op.drop_table("payments")
    op.drop_table("audit_logs")
