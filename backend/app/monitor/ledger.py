"""Read-only view of the payment ledger for the Monitor. Payment state is never taken from an event."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.payment import Payment, PaymentStatus
from app.monitor.events.completion import PaymentSummary

_OPEN = frozenset({PaymentStatus.CREATED.value, PaymentStatus.AUTHORIZED.value})
_COMMITTED = frozenset({PaymentStatus.AUTHORIZED.value, PaymentStatus.CAPTURED.value})


async def statuses(session: AsyncSession, run_id: str) -> list[str]:
    return list((await session.execute(select(Payment.status).where(Payment.run_id == run_id))).scalars())


def summarize(payment_statuses: list[str]) -> PaymentSummary:
    return PaymentSummary(
        settled=PaymentStatus.CAPTURED.value in payment_statuses,
        open_count=sum(1 for s in payment_statuses if s in _OPEN),
    )


def is_committed(payment_statuses: list[str]) -> bool:
    """Money is held or taken: a replan must release or refund it before buying again."""
    return any(s in _COMMITTED for s in payment_statuses)
