"""monitor_events: deduplicated external events with their processing outcome

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "monitor_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("problem", sa.String(32)),
        sa.Column("reason", sa.String(256)),
        sa.Column("payload", JSON_TYPE),
        sa.PrimaryKeyConstraint("id", name="pk_monitor_events"),
        sa.UniqueConstraint("source", "event_id", name="uq_monitor_events_source"),
    )
    op.create_index(
        "ix_monitor_events_run_id_entity_id_occurred_at", "monitor_events", ["run_id", "entity_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_table("monitor_events")
