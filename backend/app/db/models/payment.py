"""Payment lifecycle ledger.

Written ONLY by deterministic payment-service code (``app.payments``). LLM agents must never
import or call these models; ``tests/unit/test_architecture.py`` enforces that.
``payment_events`` and ``payment_reconciliations`` are append-only (DB triggers on PostgreSQL).
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JsonType, utcnow


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    VOIDED = "voided"
    REFUNDED = "refunded"
    FAILED = "failed"


# Deterministic lifecycle. Terminal states have no outgoing transitions.
ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({PaymentStatus.AUTHORIZED, PaymentStatus.FAILED, PaymentStatus.VOIDED}),
    PaymentStatus.AUTHORIZED: frozenset({PaymentStatus.CAPTURED, PaymentStatus.VOIDED, PaymentStatus.FAILED}),
    PaymentStatus.CAPTURED: frozenset({PaymentStatus.REFUNDED}),
    PaymentStatus.REFUNDED: frozenset(),
    PaymentStatus.VOIDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
}


def can_transition(current: PaymentStatus, target: PaymentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.CREATED.value, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(128))  # PayPal order id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("idempotency_key"),)


class PaymentEvent(Base):
    """Immutable event per lifecycle step; the source of truth for payment history."""

    __tablename__ = "payment_events"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)  # PayPal-Request-Id / correlation id
    provider_ref: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("payment_id", "request_id", "event_type"),
        Index("ix_payment_events_payment_id_created_at", "payment_id", "created_at"),
    )


class PaymentReconciliation(Base):
    """Append-only comparison of our ledger status against the provider's reported status."""

    __tablename__ = "payment_reconciliations"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"), nullable=False, index=True)
    ledger_status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_status: Mapped[str] = mapped_column(String(32), nullable=False)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
