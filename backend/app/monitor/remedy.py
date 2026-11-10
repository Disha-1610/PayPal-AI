"""Refund and dispute handling, decided by deterministic permission checks, never by an LLM.

Both go through ``evaluate_authorization``: denied by default, the user's platform/currency/spend limits
apply, and an action listed in ``approval_required_actions`` needs a verified, signed approval.
Refunds execute through ``PaymentService`` (idempotent, audited, ledger-checked). Opening a dispute has
no merchant-side PayPal API, so it stops at a recorded, authorized *preparation* the user can submit.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditEvent, record_critical
from app.db.models.payment import Payment
from app.payments.service import PaymentNotAuthorized, PaymentPermit, PaymentService
from app.schemas.authorization import (
    Action,
    AuthorizationDecision,
    evaluate_authorization,
)


class RemedyOutcome(StrEnum):
    EXECUTED = "executed"  # refund done
    PREPARED = "prepared"  # dispute authorized and recorded; the user submits it
    NEEDS_APPROVAL = "needs_approval"  # permitted in principle; a signed user approval is missing
    DENIED = "denied"


class RemedyDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: RemedyOutcome
    reason: str


async def request_refund(
    session: AsyncSession, service: PaymentService, payment_id: UUID, *, permit: PaymentPermit, reason: str
) -> RemedyDecision:
    """Check REQUEST_REFUND, then let the service check EXECUTE_REFUND and refund."""
    payment = await session.get(Payment, payment_id)
    gate = await _check(session, payment, permit, Action.REQUEST_REFUND, reason)
    if gate is not None:
        return gate
    try:
        await service.refund(session, payment_id, permit=permit, reason=reason)
    except PaymentNotAuthorized as exc:
        await _audit(session, payment, Action.EXECUTE_REFUND, "deny", str(exc))
        outcome = RemedyOutcome.NEEDS_APPROVAL if exc.requires_approval else RemedyOutcome.DENIED
        return RemedyDecision(outcome=outcome, reason=str(exc))
    await _audit(session, payment, Action.EXECUTE_REFUND, "allow", reason)
    return RemedyDecision(outcome=RemedyOutcome.EXECUTED, reason=reason)


async def prepare_dispute(
    session: AsyncSession, payment_id: UUID, *, permit: PaymentPermit, reason: str
) -> RemedyDecision:
    payment = await session.get(Payment, payment_id)
    gate = await _check(session, payment, permit, Action.OPEN_DISPUTE, reason)
    if gate is not None:
        return gate
    await _audit(session, payment, Action.OPEN_DISPUTE, "allow", reason)
    return RemedyDecision(outcome=RemedyOutcome.PREPARED, reason=reason)


async def _check(
    session: AsyncSession, payment: Payment | None, permit: PaymentPermit, action: Action, reason: str
) -> RemedyDecision | None:
    """``None`` when the action is allowed; otherwise the refusal (already audited)."""
    grant = permit.authorization
    if payment is None or (payment.run_id, payment.authorization_id) != (grant.run_id, grant.authorization_id):
        return RemedyDecision(outcome=RemedyOutcome.DENIED, reason="payment does not belong to this authorization")
    decision: AuthorizationDecision = evaluate_authorization(
        grant, action=action, platform=permit.platform, amount=payment.amount, currency=payment.currency,
        approvals=list(permit.approvals), approval_verifier=permit.verifier,
    )
    if decision.allowed:
        return None
    await _audit(session, payment, action, "deny", decision.reason)
    outcome = RemedyOutcome.NEEDS_APPROVAL if decision.requires_approval else RemedyOutcome.DENIED
    return RemedyDecision(outcome=outcome, reason=decision.reason)


async def _audit(session: AsyncSession, payment: Payment | None, action: Action, decision: str, reason: str) -> None:
    if payment is None:
        return
    await record_critical(
        session,
        AuditEvent(
            run_id=payment.run_id, node="monitor", actor_type="system", action=f"REMEDY_{action.value.upper()}",
            resource_type="payment", resource_id=str(payment.id), decision=decision, reason=reason[:512],
        ),
    )
