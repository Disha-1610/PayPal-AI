from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, utcnow


class AuditLog(Base):
    """Append-only audit record. UPDATE/DELETE are blocked by DB triggers on PostgreSQL.

    Deliberately omits IP address / user agent (not needed; avoids collecting personal data).
    """

    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)  # user | agent | system | payment_service
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. CONSTRAINT_UPDATED
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    input_hash: Mapped[str | None] = mapped_column(String(64))  # sha256 of node input
    decision: Mapped[str | None] = mapped_column(String(32))  # allow | deny | approve | ...
    reason: Mapped[str | None] = mapped_column(String(512))
    state_diff: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JsonType)

    __table_args__ = (
        Index("ix_audit_logs_run_id_timestamp", "run_id", "timestamp"),
        Index("ix_audit_logs_action", "action"),
    )
