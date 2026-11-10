"""Monitor event log: every external event the Monitor saw, with the outcome it received.

``(source, event_id)`` is unique, so a retried webhook (PayPal retries up to 25 times) can never be
applied twice, even under concurrent delivery. Progress is *derived* from applied rows ordered by
``(occurred_at, rank)``, so concurrent writers cannot leave the latest state wrong.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, utcnow


class MonitorEventRecord(Base):
    __tablename__ = "monitor_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # applied | stale | rejected
    problem: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(256))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)

    __table_args__ = (
        UniqueConstraint("source", "event_id"),
        Index("ix_monitor_events_run_id_entity_id_occurred_at", "run_id", "entity_id", "occurred_at"),
    )
