"""Chat transcript of a run: the user's messages and the agent's questions. ``(run_id, id)`` is the key, so a
retried send (same client id) and a re-observed question (same text) are stored once."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class RunMessage(Base):
    __tablename__ = "run_messages"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | agent | system
    agent: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str | None] = mapped_column(String(128))
    is_question: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
