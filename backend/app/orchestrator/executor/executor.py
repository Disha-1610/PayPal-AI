"""Phase 5 executor: recheck -> place order -> settle, for any domain.

Deterministic orchestration only. The three methods map onto graph nodes separated by the two user
interrupts (platform approval, then payer approval at the provider):

    recheck()       node "recheck"   -> user approves the exact terms (interrupt) ->
    place_order()   node "execute"   -> payer approves at the provider (interrupt) ->
    settle()        node "settle"    -> capture / void

Domain differences live behind two small protocols (``TermsObserver``, ``EvidenceSource``) and the
plug-in's declarative ``VerificationSpec``; approval, recheck, ledger and audit are shared.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import Phase
from app.audit.service import AuditEvent, hash_input, record_critical
from app.orchestrator.agents.verifier import verify_evidence
from app.orchestrator.executor.mismatch import MismatchPolicy, classify_mismatch
from app.orchestrator.executor.state import ExecutionState, advance
from app.payments.service import CreatedPayment, PaymentPermit, PaymentService
from app.schemas.approval_terms import ApprovalTerms, from_minor_units
from app.schemas.authorization import Approval
from app.schemas.recheck import MismatchAction, MismatchReport, ObservedTerms
from app.schemas.verification import (
    Evidence,
    VerificationResult,
    VerificationSpec,
    VerificationVerdict,
)


class TermsObserver(Protocol):
    """Reads the live state of the approved subject from its source (connector, marketplace, agent)."""

    async def observe(self, terms: ApprovalTerms) -> ObservedTerms: ...


class EvidenceSource(Protocol):
    """Collects fulfilment evidence for an order (merchant order status, milestone, agent log)."""

    async def collect(self, terms: ApprovalTerms, order_id: str) -> Evidence: ...


class ExecutionBlocked(Exception):
    """The approved terms no longer hold. ``report`` says why and where to route (reapprove, Adapt, Evaluator)."""

    def __init__(self, report: MismatchReport) -> None:
        super().__init__(f"execution blocked: {report.action.name}")
        self.report = report


@dataclass(frozen=True)
class Rechecked:
    state: ExecutionState
    report: MismatchReport


@dataclass(frozen=True)
class OrderPlaced:
    state: ExecutionState
    created: CreatedPayment


@dataclass(frozen=True)
class Settlement:
    state: ExecutionState
    verification: VerificationResult | None = None
    report: MismatchReport | None = None
    captured: Decimal | None = None


class Executor:
    def __init__(
        self,
        payments: PaymentService,
        observer: TermsObserver,
        evidence: EvidenceSource,
        spec: VerificationSpec | None = None,
    ) -> None:
        spec = spec or VerificationSpec()
        self._payments = payments
        self._observer = observer
        self._evidence = evidence
        self._spec = spec
        self._policy = MismatchPolicy(tolerated_fields=spec.tolerated_fields)

    # ---- 1. recheck (before asking the user to approve) ---------------------------------------

    async def recheck(self, session: AsyncSession, *, run_id: str, terms: ApprovalTerms) -> Rechecked:
        """Compare the terms about to be shown to the user with the live source. Nothing is executed."""
        report = classify_mismatch(terms, await self._observer.observe(terms), self._policy)
        await _audit(session, run_id, "EXECUTION_RECHECK", report.action.name, terms, report)
        state = advance(ExecutionState.PROPOSED, ExecutionState.RECHECKED) if report.can_proceed else ExecutionState.PROPOSED
        return Rechecked(state, report)

    # ---- 2. place the order (after the user approved) -----------------------------------------

    async def place_order(
        self,
        session: AsyncSession,
        *,
        permit: PaymentPermit,
        approval: Approval,
        terms: ApprovalTerms,
        order_id: str,
        payee_id: str,
    ) -> OrderPlaced:
        """Re-verify the live terms, then create the order for the verified amount. Idempotent per approval."""
        if not permit.verifier(approval) or approval.terms_hash != terms.terms_hash():
            raise ExecutionBlocked(MismatchReport(action=MismatchAction.REAPPROVE))
        observed = await self._observer.observe(terms)
        report = classify_mismatch(terms, observed, self._policy)
        await _audit(session, approval.run_id, "EXECUTION_PRE_ORDER_RECHECK", report.action.name, terms, report)
        if not report.can_proceed:
            raise ExecutionBlocked(report)
        # Authorize and hold at the live total, never above what the user approved.
        total = from_minor_units(_known(observed.total_minor), terms.currency)
        created = await self._payments.create(
            session, permit=permit, approval=approval, order_id=order_id, amount=total, payee_id=payee_id
        )
        state = ExecutionState.APPROVED
        for step in (ExecutionState.EXECUTING, ExecutionState.AWAITING_CONFIRMATION):
            state = advance(state, step)
        return OrderPlaced(state, created)

    # ---- 3. settle (after the payer approved at the provider) ---------------------------------

    async def settle(
        self,
        session: AsyncSession,
        payment_id: UUID,
        *,
        permit: PaymentPermit,
        terms: ApprovalTerms,
        order_id: str,
        attempt: int = 1,
    ) -> Settlement:
        """Second recheck, authorize, verify delivery, then capture (PASS) or void (FAIL / out of attempts).

        Raises ``PaymentNotReady`` while the payer has not approved: the caller waits and calls again.
        An INSUFFICIENT_EVIDENCE result keeps the hold and returns AWAITING_CONFIRMATION; call again with
        ``attempt + 1``. After ``spec.max_attempts`` the hold is released.
        """
        run_id = permit.authorization.run_id
        report = classify_mismatch(terms, await self._observer.observe(terms), self._policy)
        await _audit(session, run_id, "EXECUTION_PRE_SETTLE_RECHECK", report.action.name, terms, report)
        # Unknown data (INSUFFICIENT_DATA) does not block holding funds: nothing is captured without PASS evidence.
        if not report.can_proceed and report.action is not MismatchAction.INSUFFICIENT_DATA:
            await self._payments.void(session, payment_id, permit=permit, reason=f"terms changed: {report.action.name}")
            return Settlement(ExecutionState.VOIDED, report=report)

        payment = await self._payments.authorize(session, payment_id, permit=permit)  # may raise PaymentNotReady
        evidence = await self._evidence.collect(terms, order_id)
        result = verify_evidence(terms, evidence, self._spec)
        await _audit(session, run_id, "EXECUTION_VERIFY", result.verdict.value, terms, result.report, result.reasons)

        if result.verdict is VerificationVerdict.INSUFFICIENT_EVIDENCE:
            if attempt < self._spec.max_attempts:
                return Settlement(ExecutionState.AWAITING_CONFIRMATION, verification=result)
            await self._payments.void(session, payment_id, permit=permit, reason="verification evidence never arrived")
            return Settlement(ExecutionState.EXPIRED, verification=result)
        if result.verdict is VerificationVerdict.FAIL:
            await self._payments.void(session, payment_id, permit=permit, reason="; ".join(result.reasons) or "verification failed")
            return Settlement(ExecutionState.VOIDED, verification=result)

        # PASS implies a known total within the approved ceiling. Capture what was verified, never more
        # than was authorized; the remainder of the hold is released by the final capture.
        verified = from_minor_units(_known(evidence.observed.total_minor), terms.currency)
        amount = min(verified, payment.amount)
        captured = await self._payments.capture(session, payment_id, permit=permit, amount=amount)
        if captured.status == "failed":  # the hold lapsed (honor/29-day window): nothing was captured
            return Settlement(ExecutionState.EXPIRED, verification=result)
        return Settlement(ExecutionState.SETTLED, verification=result, captured=amount)


async def _audit(
    session: AsyncSession,
    run_id: str,
    action: str,
    decision: str,
    terms: ApprovalTerms,
    report: MismatchReport,
    reasons: tuple[str, ...] = (),
) -> None:
    await record_critical(
        session,
        AuditEvent(
            run_id=run_id, phase=Phase.EXECUTION, node="executor", actor_type="system", action=action, resource_type="approval_terms",
            resource_id=terms.terms_hash(), input_hash=hash_input(report.model_dump(mode="json")), decision=decision,
            reason="; ".join(reasons)[:512] or None,
            metadata_json={"changes": [c.model_dump(mode="json") for c in report.changes]},
        ),
    )


def _known(value: int | None) -> int:
    if value is None:
        raise ExecutionBlocked(MismatchReport(action=MismatchAction.INSUFFICIENT_DATA))
    return value


__all__ = ["EvidenceSource", "ExecutionBlocked", "Executor", "OrderPlaced", "Rechecked", "Settlement", "TermsObserver"]
