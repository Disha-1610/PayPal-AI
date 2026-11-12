"""The counterparty a contractor / A2A run is waiting on. One row per run: the contact the user named, what was last
sent (a quote request or a counter-offer), and the reply deadline the sweeper watches. ``phone`` and ``email`` are
indexed so an inbound SMS / email (which carries no run id) finds its run."""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class RunCounterparty(Base):
    __tablename__ = "run_counterparties"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    contact: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(24))
    stage: Mapped[str] = mapped_column(String(16), nullable=False)  # quote | counter | idle
    round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    awaiting: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reply_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_offer: Mapped[dict[str, object] | None] = mapped_column(JSON)  # our latest counter-offer (an ACCEPT reply takes it)
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_run_counterparties_deadline", "awaiting", "reply_deadline"),
        Index("ix_run_counterparties_phone", "phone"),
        Index("ix_run_counterparties_email", "email"),
    )
