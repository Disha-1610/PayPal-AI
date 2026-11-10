"""Contractor milestones: one row per milestone, each with its own payment lifecycle.

Amount, due date and acceptance criteria are fixed when the user approves the plan (``plan_hash``) and
bound again into the per-milestone approval (``terms_hash``); only ``app.domains.contractor.milestone``
writes this table. The payment itself lives in ``payments`` (linked by ``payment_id``).
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, utcnow


class MilestoneStatus(StrEnum):
    PENDING = "pending"  # approved, not started
    ORDER_PLACED = "order_placed"  # payment created; awaiting payer approval, delivery and verification
    SETTLED = "settled"  # verified and captured
    VOIDED = "voided"  # verification failed or terms changed; hold released
    EXPIRED = "expired"  # evidence never arrived or the hold lapsed


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class Milestone(Base):
    __tablename__ = "milestones"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    contractor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JsonType, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    terms_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=MilestoneStatus.PENDING.value, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(16), default=VerificationStatus.PENDING.value, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payment_id: Mapped[UUID | None] = mapped_column(ForeignKey("payments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("run_id", "project_id", "sequence"),)
