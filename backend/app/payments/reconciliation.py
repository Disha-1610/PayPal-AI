"""Payment reconciliation: compare the ledger with what PayPal says, and record every comparison.

Webhooks tell the Monitor what happened; they never change the ledger. This worker is the other half: it reads
PayPal's API (the trusted source) for each open payment and appends a ``PaymentReconciliation`` row.

* Agreement is recorded and nothing else happens.
* The one safe disagreement is repaired: the ledger says AUTHORIZED but PayPal released the hold (voided/expired).
  That is ledger-only (``PaymentService.fail_if_hold_released``), so the run can re-source.
* Every other disagreement involves money (captured at PayPal but not in the ledger, or a hold PayPal still keeps
  for a voided payment). It is recorded and logged as an error for a person; nothing is moved automatically.
* An unreachable PayPal is not a mismatch: that payment is skipped and checked again on the next pass.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models.payment import Payment, PaymentEvent, PaymentReconciliation, PaymentStatus
from app.db.session import session_scope
from app.payments.provider import PaymentProvider, ProviderError
from app.payments.service import PaymentService

logger = get_logger(__name__)

_AUTH_EVENTS = ("authorized", "reauthorized")
# ledger status -> provider statuses that agree with it
_AGREES: dict[str, frozenset[str]] = {
    PaymentStatus.CREATED.value: frozenset({"created", "payer_approved"}),  # an order that is not yet authorized
    PaymentStatus.AUTHORIZED.value: frozenset({"created"}),  # an authorization that is open and uncaptured
    PaymentStatus.CAPTURED.value: frozenset({"captured", "partially_captured"}),
}
_RELEASED = frozenset({"voided", "expired"})


@dataclass(frozen=True)
class ReconcileSummary:
    checked: int = 0
    matched: int = 0
    repaired: int = 0
    mismatched: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class _Observed:
    kind: str  # "authorization" | "order"
    status: str
    detail: dict[str, Any]


def agrees(ledger_status: str, observed: _Observed, *, had_authorization: bool) -> bool:
    if ledger_status == PaymentStatus.VOIDED.value:
        if had_authorization:  # the hold must be gone
            return observed.kind == "authorization" and observed.status in _RELEASED
        return observed.status != "authorized"  # voided before any hold existed: PayPal must not hold funds
    return observed.status in _AGREES.get(ledger_status, frozenset())


class Reconciler:
    def __init__(self, provider: PaymentProvider, payments: PaymentService, *, min_interval: timedelta = timedelta(minutes=5)) -> None:
        self._provider, self._payments, self._min_interval = provider, payments, min_interval

    async def sweep(self, factory: async_sessionmaker[AsyncSession], *, limit: int = 200, now: datetime | None = None) -> ReconcileSummary:
        """One pass over payments that can still disagree. Safe to overlap or repeat: rows are append-only."""
        now = now or utcnow()
        async with factory() as session:
            ids = await self._due(session, now, limit)
        totals = dict(checked=0, matched=0, repaired=0, mismatched=0, skipped=0)
        for payment_id in ids:
            try:
                async with session_scope(factory) as session:
                    outcome = await self.reconcile(session, payment_id)
            except ProviderError as exc:
                logger.warning("reconciliation skipped: provider unreachable", extra={"payment_id": str(payment_id), "reason": str(exc)})
                totals["skipped"] += 1
                continue
            totals["checked"] += 1
            totals[outcome] += 1
        return ReconcileSummary(**totals)

    async def reconcile(self, session: AsyncSession, payment_id: Any) -> str:
        """Compare one payment and record the result. Returns ``matched``, ``repaired`` or ``mismatched``."""
        payment = await session.get(Payment, payment_id)
        if payment is None:
            raise LookupError(payment_id)
        auth_id = await self._authorization_id(session, payment.id)
        observed = await self._observe(payment, auth_id)
        ledger_before = payment.status
        ok = agrees(ledger_before, observed, had_authorization=auth_id is not None)
        outcome = "matched" if ok else "mismatched"
        detail: dict[str, Any] = {**observed.detail, "source": observed.kind}
        if not ok and ledger_before == PaymentStatus.AUTHORIZED.value and observed.status in _RELEASED:
            if await self._payments.fail_if_hold_released(session, payment.id):
                outcome = "repaired"
                detail["repair"] = "ledger failed: hold released at provider"
        if outcome == "mismatched":
            logger.error(
                "payment ledger disagrees with the provider", extra={"run_id": payment.run_id, "payment_id": str(payment.id),
                                                                     "ledger": ledger_before, "provider": observed.status},
            )
        session.add(
            PaymentReconciliation(
                payment_id=payment.id, ledger_status=ledger_before, provider_status=observed.status, matched=ok, detail=detail,
            )
        )
        return outcome

    async def _observe(self, payment: Payment, authorization_id: str | None) -> _Observed:
        if authorization_id is not None:
            auth = await self._provider.get_authorization(authorization_id)
            return _Observed("authorization", auth.status, {"authorization_id": authorization_id})
        if payment.provider_order_id is None:
            return _Observed("order", "created", {"note": "no provider order yet"})
        order = await self._provider.get_order(payment.provider_order_id)
        return _Observed("order", order.status, {"order_id": payment.provider_order_id})

    async def _authorization_id(self, session: AsyncSession, payment_id: Any) -> str | None:
        stmt = (
            select(PaymentEvent.provider_ref)
            .where(PaymentEvent.payment_id == payment_id, PaymentEvent.event_type.in_(_AUTH_EVENTS))
            .order_by(PaymentEvent.created_at.desc())
        )
        return (await session.execute(stmt)).scalars().first()

    async def _due(self, session: AsyncSession, now: datetime, limit: int) -> list[Any]:
        """Open payments, plus recently settled ones, that were not checked within ``min_interval``."""
        recent = now - timedelta(days=7)
        open_or_recent = or_(
            Payment.status.in_((PaymentStatus.CREATED.value, PaymentStatus.AUTHORIZED.value)),
            (Payment.status.in_((PaymentStatus.CAPTURED.value, PaymentStatus.VOIDED.value)) & (Payment.updated_at >= recent)),
        )
        last_checked = (
            select(PaymentReconciliation.payment_id)
            .where(PaymentReconciliation.checked_at >= now - self._min_interval)
        )
        stmt = select(Payment.id).where(open_or_recent, Payment.id.not_in(last_checked)).order_by(Payment.updated_at).limit(limit)
        return list((await session.execute(stmt)).scalars())
