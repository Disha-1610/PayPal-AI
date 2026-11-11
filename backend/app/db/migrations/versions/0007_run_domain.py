"""run domain

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("domain", sa.String(32), nullable=False, server_default="shopping"))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("domain")
