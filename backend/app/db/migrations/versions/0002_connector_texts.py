"""connector_texts: untrusted review/listing text, referenced by id only

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_texts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("rating", sa.Float()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_connector_texts"),
    )
    op.create_index("ix_connector_texts_run_id_subject_id", "connector_texts", ["run_id", "subject_id"])


def downgrade() -> None:
    op.drop_table("connector_texts")
