"""Payment service: the only code that moves money or writes the payment ledger.

Flow (``intent=AUTHORIZE``): ``create`` -> payer approves at the provider -> ``authorize`` ->
(deterministic verification elsewhere) -> ``capture`` or ``void``.

Guarantees:
  * Gated: create, authorize and capture each run ``evaluate_authorization`` with a ``PaymentPermit``.
    The action must be permitted, within ``max_spend``, and backed by a *verified* approval for that
    exact action. ``void`` only releases funds, so it needs no approval, but the payment must still
    belong to the permit's run and authorization.
  * Idempotent: the key is derived from the approval/payment id, never random, and is sent to the
    provider. Retrying a call after a crash or timeout cannot double-create or double-capture.
  * Serialized: state changes lock the payment row (SELECT ... FOR UPDATE); concurrent creators for
    one approval converge on one payment.
  * Ledger transitions follow ``ALLOWED_TRANSITIONS``; each writes an append-only ``PaymentEvent``
    and a critical audit row in the caller's transaction.
  * The provider's order is compared with the ledger before authorizing (amount, currency, payee;
    a missing payee fails closed).
  * Capture never exceeds the authorized amount.
  * If a provider call succeeds but the response is lost or the DB commit fails, the ledger lags the
    provider. Capture and void re-read the authorization from PayPal on any provider error and record
    the outcome if it was already applied; authorize re-reads the order first. Create and authorize
    retries replay the same idempotency key. A background reconciliation worker for crashes that
    leave no error to react to (``payment_reconciliations``) is still to be wired.

LLM agents never import this module.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditEvent, record_critical
from app.core.logging import get_logger
from app.db.models.payment import Payment, PaymentEvent, PaymentStatus, can_transition
from app.payments.provider import PaymentProvider, ProviderError, ProviderOrder
from app.schemas.approval_terms import to_minor_units
from app.schemas.authorization import (
    Action,
    Approval,
    ApprovalVerifier,
    TaskAuthorization,
    evaluate_authorization,
)

logger = get_logger(__name__)

_ORDER_CREATED = "order_created"
_AUTHORIZED = "authorized"
_CAPTURED = "captured"
_VOIDED = "voided"


class PaymentServiceError(Exception):
    """A payment operation was refused. Nothing was changed at the provider unless stated."""


class PaymentNotAuthorized(PaymentServiceError):
    """The permit does not allow this action (not permitted, over limit, or no verified approval)."""


class PaymentMismatch(PaymentServiceError):
    """The provider's order differs from what the ledger/approval says. Do not authorize."""


class PaymentNotReady(PaymentServiceError):
    """The payer has not approved yet, or the provider has not settled; safe to retry later."""


@dataclass(frozen=True)
class PaymentPermit:
    """What the caller may do: the task grant, its platform, and the approvals to check."""

    authorization: TaskAuthorization
    platform: str
    approvals: tuple[Approval, ...]
    verifier: ApprovalVerifier


class CreatedPayment(NamedTuple):
    payment: Payment
    approval_url: str | None


class PaymentService:
    def __init__(self, provider: PaymentProvider) -> None:
        self._provider = provider

    # ---- create -------------------------------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        *,
        permit: PaymentPermit,
        approval: Approval,
        order_id: str,
        amount: Decimal,
        payee_id: str,
    ) -> CreatedPayment:
        """Create the ledger row and the provider order. Idempotent per approval; single-use per approval."""
        self._authorize_action(permit, Action.CREATE_ORDER, amount, approval.currency)
        if approval not in permit.approvals or approval.action is not Action.CREATE_ORDER or not permit.verifier(approval):
            raise PaymentNotAuthorized("approval is not a verified create_order approval of this permit")
        key = f"{approval.approval_id}:create"
        payment = await self._get_or_insert(
            session,
            key,
            Payment(
                run_id=approval.run_id,
                order_id=order_id,
                authorization_id=approval.authorization_id,
                approval_id=approval.approval_id,
                amount=amount,
                currency=approval.currency,
                idempotency_key=key,
            ),
        )
        if (payment.amount, payment.currency) != (amount, approval.currency):
            raise PaymentServiceError("this approval was already used for a different amount")
        if payment.provider_order_id:
            order = await self._provider.get_order(payment.provider_order_id)
            return CreatedPayment(payment, order.approval_url)
        order = await self._provider.create_order(
            request_id=key, amount=amount, currency=approval.currency, payee_id=payee_id, reference=str(payment.id)
        )
        _check_order(order, amount=amount, currency=approval.currency, payee_id=payee_id)
        payment.provider_order_id = order.order_id
        await self._record(
            session, payment, event_type=_ORDER_CREATED, from_status=None, to_status=PaymentStatus.CREATED,
            request_id=key, provider_ref=order.order_id, payload={"payee_id": payee_id},
        )
        return CreatedPayment(payment, order.approval_url)

    # ---- authorize ----------------------------------------------------------------------------

    async def authorize(self, session: AsyncSession, payment_id: UUID, *, permit: PaymentPermit) -> Payment:
        """After the payer approved at the provider: re-check the order, then authorize (hold funds)."""
        payment = await self._load(session, payment_id, permit)
        if payment.status == PaymentStatus.AUTHORIZED.value:
            return payment  # idempotent: a settle retry after "evidence pending" must not re-authorize
        if payment.status != PaymentStatus.CREATED.value:
            raise PaymentServiceError(f"payment is {payment.status}, expected created")
        self._authorize_action(permit, Action.AUTHORIZE_PAYMENT, payment.amount, payment.currency)
        created = await self._event(session, payment.id, _ORDER_CREATED)
        payee_id = (created.payload or {}).get("payee_id") if created else None
        if payee_id is None:
            raise PaymentMismatch("the approved payee was not recorded for this payment")
        order = await self._provider.get_order(_required(payment.provider_order_id))
        _check_order(order, amount=payment.amount, currency=payment.currency, payee_id=payee_id)
        if order.authorization_id is None:
            if order.status != "payer_approved":
                raise PaymentNotReady(f"order is {order.status}, payer has not approved")
            order = await self._provider.authorize_order(request_id=f"{payment.id}:authorize", order_id=order.order_id)
        authorization_id = _required(order.authorization_id, "provider returned no authorization id")
        authorization = await self._provider.get_authorization(authorization_id)
        if (authorization.amount, authorization.currency) != (payment.amount, payment.currency):
            raise PaymentMismatch("authorized amount differs from the approved amount")
        await self._record(
            session, payment, event_type=_AUTHORIZED, from_status=PaymentStatus.CREATED, to_status=PaymentStatus.AUTHORIZED,
            request_id=f"{payment.id}:authorize", provider_ref=authorization_id,
        )
        return payment

    # ---- capture / void -----------------------------------------------------------------------

    async def capture(
        self, session: AsyncSession, payment_id: UUID, *, permit: PaymentPermit, amount: Decimal | None = None
    ) -> Payment:
        """Capture ``amount`` (default: everything authorized). A final capture releases any remainder."""
        payment = await self._load(session, payment_id, permit, PaymentStatus.AUTHORIZED)
        amount = payment.amount if amount is None else amount
        if amount <= 0 or amount > payment.amount:
            raise PaymentServiceError("capture amount must be positive and within the authorized amount")
        self._authorize_action(permit, Action.CAPTURE_PAYMENT, amount, payment.currency)
        authorization_id = await self._authorization_id(session, payment.id)
        capture_id: str | None = None
        recovered = False
        try:
            capture = await self._provider.capture(
                request_id=f"{payment.id}:capture", authorization_id=authorization_id, amount=amount,
                currency=payment.currency, final_capture=True,
            )
        except ProviderError as exc:
            # The capture may have been applied even though we saw an error: read PayPal before giving up.
            if not await self._settled_at_provider(authorization_id, {"captured", "partially_captured"}, exc):
                raise
            recovered = True
        else:
            if capture.status != "completed":
                raise PaymentNotReady(f"capture is {capture.status}; ledger left AUTHORIZED")
            capture_id = capture.capture_id
        await self._record(
            session, payment, event_type=_CAPTURED, from_status=PaymentStatus.AUTHORIZED, to_status=PaymentStatus.CAPTURED,
            request_id=f"{payment.id}:capture", provider_ref=capture_id,
            payload={
                "captured_minor": to_minor_units(amount, payment.currency),
                "authorized_minor": to_minor_units(payment.amount, payment.currency),
                **({"recovered_from_provider": True} if recovered else {}),
            },
        )
        return payment

    async def void(self, session: AsyncSession, payment_id: UUID, *, permit: PaymentPermit, reason: str) -> Payment:
        """Release an unsettled payment. Before authorization nothing is held, so only the ledger changes."""
        payment = await self._load(session, payment_id, permit)
        status = PaymentStatus(payment.status)
        if status is PaymentStatus.AUTHORIZED:
            authorization_id = await self._authorization_id(session, payment.id)
            try:
                await self._provider.void(request_id=f"{payment.id}:void", authorization_id=authorization_id)
            except ProviderError as exc:
                # A lost response or a replayed void: if PayPal already released the hold, the goal is met.
                if not await self._settled_at_provider(authorization_id, {"voided"}, exc):
                    raise
        elif status is not PaymentStatus.CREATED:
            raise PaymentServiceError(f"cannot void a payment in status {status.value}")
        await self._record(
            session, payment, event_type=_VOIDED, from_status=status, to_status=PaymentStatus.VOIDED,
            request_id=f"{payment.id}:void", payload={"reason": reason},
        )
        return payment

    # ---- internals ----------------------------------------------------------------------------

    def _authorize_action(self, permit: PaymentPermit, action: Action, amount: Decimal, currency: str) -> None:
        decision = evaluate_authorization(
            permit.authorization, action=action, platform=permit.platform, amount=amount, currency=currency,
            approvals=list(permit.approvals), approval_verifier=permit.verifier,
        )
        if not decision.allowed:
            raise PaymentNotAuthorized(f"{action.value} denied: {decision.reason}")

    async def _settled_at_provider(self, authorization_id: str, wanted: set[str], original: ProviderError) -> bool:
        """After a failed capture/void, ask PayPal whether the authorization already reached ``wanted``.

        A timeout is not a failure, so the provider is the source of truth. If this read also fails, the
        original error is re-raised (still uncertain, ledger untouched, safe to retry later).
        """
        try:
            authorization = await self._provider.get_authorization(authorization_id)
        except ProviderError:
            raise original from None
        logger.warning(
            "payment reconciled after provider error",
            extra={"authorization_id": authorization_id, "provider_status": authorization.status},
        )
        return authorization.status in wanted

    async def _load(
        self, session: AsyncSession, payment_id: UUID, permit: PaymentPermit, expected: PaymentStatus | None = None
    ) -> Payment:
        """Load and lock the payment row (SELECT ... FOR UPDATE) so concurrent transitions serialize."""
        payment = (
            await session.execute(select(Payment).where(Payment.id == payment_id).with_for_update())
        ).scalar_one_or_none()
        if payment is None:
            raise PaymentServiceError("payment not found")
        grant = permit.authorization
        if (payment.run_id, payment.authorization_id) != (grant.run_id, grant.authorization_id):
            raise PaymentNotAuthorized("payment does not belong to this authorization")
        if expected is not None and payment.status != expected.value:
            raise PaymentServiceError(f"payment is {payment.status}, expected {expected.value}")
        return payment

    async def _get_or_insert(self, session: AsyncSession, key: str, new: Payment) -> Payment:
        """Return the payment for an idempotency key, inserting ``new`` if absent (safe under concurrent creators)."""
        existing = (await session.execute(select(Payment).where(Payment.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            return existing
        try:
            async with session.begin_nested():
                session.add(new)
                await session.flush()
        except IntegrityError:  # a concurrent creator inserted the same key first
            winner = (await session.execute(select(Payment).where(Payment.idempotency_key == key))).scalar_one_or_none()
            if winner is None:
                raise
            return winner
        return new

    async def _event(self, session: AsyncSession, payment_id: UUID, event_type: str) -> PaymentEvent | None:
        stmt = (
            select(PaymentEvent)
            .where(PaymentEvent.payment_id == payment_id, PaymentEvent.event_type == event_type)
            .order_by(PaymentEvent.created_at.desc())
        )
        return (await session.execute(stmt)).scalars().first()

    async def _authorization_id(self, session: AsyncSession, payment_id: UUID) -> str:
        event = await self._event(session, payment_id, _AUTHORIZED)
        return _required(event.provider_ref if event else None, "no provider authorization recorded")

    async def _record(
        self,
        session: AsyncSession,
        payment: Payment,
        *,
        event_type: str,
        from_status: PaymentStatus | None,
        to_status: PaymentStatus,
        request_id: str,
        provider_ref: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        if from_status is not None and not can_transition(from_status, to_status):
            raise PaymentServiceError(f"illegal transition {from_status.value} -> {to_status.value}")
        payment.status = to_status.value
        session.add(
            PaymentEvent(
                payment_id=payment.id, event_type=event_type, from_status=from_status.value if from_status else None,
                to_status=to_status.value, request_id=request_id, provider_ref=provider_ref, payload=payload,
            )
        )
        await record_critical(
            session,
            AuditEvent(
                run_id=payment.run_id, node="payment_service", actor_type="payment_service", action=f"PAYMENT_{event_type.upper()}",
                resource_type="payment", resource_id=str(payment.id), decision="allow",
                metadata_json={"status": to_status.value, "approval_id": payment.approval_id, "provider_ref": provider_ref},
            ),
        )
        logger.info("payment transition", extra={"run_id": payment.run_id, "payment_id": str(payment.id), "status": to_status.value})


def _required[T](value: T | None, message: str = "missing value") -> T:
    if value is None:
        raise PaymentServiceError(message)
    return value


def _check_order(order: ProviderOrder, *, amount: Decimal, currency: str, payee_id: str) -> None:
    """Fail closed: a provider order that omits its payee cannot be shown to match the approved one."""
    if (order.amount, order.currency) != (amount, currency):
        raise PaymentMismatch(f"provider order {order.amount} {order.currency} != approved {amount} {currency}")
    if order.payee_id != payee_id:
        raise PaymentMismatch("provider order payee differs from (or is missing from) the approved payee")


__all__ = [
    "CreatedPayment",
    "PaymentMismatch",
    "PaymentNotAuthorized",
    "PaymentNotReady",
    "PaymentPermit",
    "PaymentService",
    "PaymentServiceError",
    "ProviderError",
]
