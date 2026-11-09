from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


class ConnectorText(Base):
    """Untrusted free text from a connector (review or listing description).

    State and prompts reference rows by ``id`` only; the body is data, never instructions.
    """

    __tablename__ = "connector_texts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)  # connector id
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)  # candidate id
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # review | listing
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[float | None] = mapped_column(Float)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (Index("ix_connector_texts_run_id_subject_id", "run_id", "subject_id"),)
