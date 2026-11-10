"""Milestone lifecycle for the contractor domain: Authorize -> Deliver -> Verify -> Capture / Void, per milestone.

Built on the shared ``Executor`` (recheck, order, verify, settle) and ``PaymentService``; nothing here
talks to PayPal or decides verification itself. What this module adds is the milestone rules:

  * The plan is persisted only if it hashes to what the user approved; a different plan is rejected
    and needs a fresh approval. Amounts, due dates and criteria are never amended in place.
  * A milestone can start only when every earlier milestone is SETTLED, so no later payment is ever
    authorized, let alone captured, before the earlier work verified.
  * Each milestone has its own approval (``ApprovalTerms`` binds amount, payee, due date and the
    criteria hash) and its own payment; capture happens only on a verifier PASS.
  * A failed milestone stays VOIDED/EXPIRED and blocks the rest; recovery or renegotiation goes back
    through Adapt and a new approved plan.

The contractor's observer/evidence source must echo ``domain_data`` (plan hash, sequence, criteria hash)
and report the milestone amount and completion date; anything else fails the shared mismatch check.

This is milestone-based authorization and capture, not escrow: PayPal holds the authorization, it does
not guarantee funds beyond its honor period (the payment service reauthorizes within the 29-day window).
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import AuditSink, Phase, emit, emit_critical
from app.core.hashing import stable_hash
from app.db.models.milestone import Milestone, MilestoneStatus, VerificationStatus
from app.domains.contractor.schemas import MilestonePlan
from app.orchestrator.executor.executor import Executor, OrderPlaced, Settlement
from app.orchestrator.executor.state import ExecutionState
from app.payments.service import PaymentPermit
from app.schemas.approval_terms import ApprovalTerms, to_minor_units
from app.schemas.authorization import Approval
from app.schemas.verification import VerificationVerdict

_NODE = "contractor_milestones"
_PERMISSION = "deliver_milestone"


class MilestoneError(Exception):
    """A milestone operation was refused. Nothing was changed."""


class MilestonePlanChanged(MilestoneError):
    """The plan or a milestone differs from what the user approved; only the user can approve a change."""


class MilestoneOutOfOrder(MilestoneError):
    """An earlier milestone has not been settled, or this one is not in the state the step needs."""


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)  # SQLite drops tzinfo


def terms_for(row: Milestone) -> ApprovalTerms:
    """The exact agreement for one milestone: what the user signs and what the verifier checks."""
    minor = to_minor_units(row.amount, row.currency)
    return ApprovalTerms(
        subject_id=f"{row.project_id}:{row.sequence}",
        parties={"payee_id": row.contractor_id},
        currency=row.currency,
        max_total_minor=minor,
        expected_total_minor=minor,
        latest_acceptable_at=_aware(row.due_date),
        permissions=frozenset({_PERMISSION}),
        domain_data={"plan_hash": row.plan_hash, "sequence": row.sequence, "criteria_hash": _criteria_hash(row)},
    )


def _criteria_hash(row: Milestone) -> str:
    return stable_hash(list(row.acceptance_criteria))


def order_reference(row: Milestone) -> str:
    return f"ms-{row.id.hex}"


class MilestoneService:
    def __init__(self, executor: Executor, audit: AuditSink | None = None) -> None:
        """``audit`` records refusals on the background writer: they raise, which rolls back the caller's
        transaction, so an in-transaction audit row for a refusal would be lost with it."""
        self._executor = executor
        self._audit_sink = audit

    # ---- create ---------------------------------------------------------------------------------

    async def create_plan(
        self, session: AsyncSession, *, run_id: str, plan: MilestonePlan, approved_plan_hash: str
    ) -> list[Milestone]:
        """Persist the approved plan. Idempotent for the same plan; any other plan is refused."""
        plan_hash = plan.plan_hash()
        if plan_hash != approved_plan_hash:
            await self._reject(session, run_id, plan.project_id, "plan differs from the approved plan", plan_hash)
        existing = await self._rows(session, run_id, plan.project_id)
        if existing:
            if existing[0].plan_hash != plan_hash:
                await self._reject(session, run_id, plan.project_id, "project already has a different approved plan", plan_hash)
            return existing

        rows = [
            Milestone(
                run_id=run_id, project_id=plan.project_id, contractor_id=plan.contractor_id, sequence=index,
                description=spec.description, amount=spec.amount, currency=plan.currency, due_date=spec.due_date,
                acceptance_criteria=list(spec.acceptance_criteria), plan_hash=plan_hash, terms_hash="",
            )
            for index, spec in enumerate(plan.milestones, start=1)
        ]
        for row in rows:
            row.terms_hash = terms_for(row).terms_hash()
            session.add(row)
        await session.flush()
        for row in rows:
            await self._audit(session, row, "MILESTONE_CREATED", decision="allow", payload=terms_for(row).canonical())
        return rows

    # ---- authorize (order) ----------------------------------------------------------------------

    async def start(
        self, session: AsyncSession, *, run_id: str, project_id: str, sequence: int, permit: PaymentPermit, approval: Approval
    ) -> OrderPlaced:
        """Place this milestone's order once every earlier milestone is settled."""
        row = await self._row(session, run_id, project_id, sequence)
        if row.status != MilestoneStatus.PENDING.value:
            raise MilestoneOutOfOrder(f"milestone {sequence} is {row.status}, expected pending")
        await self._require_prior_settled(session, row)
        terms = await self._checked_terms(session, row)
        placed = await self._executor.place_order(
            session, permit=permit, approval=approval, terms=terms, order_id=order_reference(row), payee_id=row.contractor_id
        )
        row.payment_id = placed.created.payment.id
        row.status = MilestoneStatus.ORDER_PLACED.value
        await self._audit(session, row, "MILESTONE_STARTED", decision="allow", payload=terms.canonical())
        return placed

    # ---- verify, then capture or void -----------------------------------------------------------

    async def settle(
        self, session: AsyncSession, *, run_id: str, project_id: str, sequence: int, permit: PaymentPermit
    ) -> Settlement:
        """Authorize, verify the deliverable, then capture (PASS) or void (FAIL). Safe to call repeatedly.

        While evidence is pending the hold is kept and the milestone stays ORDER_PLACED; after the
        verification spec's attempt limit the hold is released. ``PaymentNotReady`` (payer has not
        approved yet) propagates and changes nothing.
        """
        row = await self._row(session, run_id, project_id, sequence)
        if row.status != MilestoneStatus.ORDER_PLACED.value or row.payment_id is None:
            raise MilestoneOutOfOrder(f"milestone {sequence} is {row.status}, expected order_placed")
        terms = await self._checked_terms(session, row)
        settlement = await self._executor.settle(
            session, row.payment_id, permit=permit, terms=terms, order_id=order_reference(row), attempt=row.attempts + 1
        )
        row.attempts += 1
        if settlement.state is not ExecutionState.AWAITING_CONFIRMATION:
            passed = settlement.verification is not None and settlement.verification.verdict is VerificationVerdict.PASS
            row.verification_status = (VerificationStatus.PASSED if passed else VerificationStatus.FAILED).value
            row.status = {
                ExecutionState.SETTLED: MilestoneStatus.SETTLED,
                ExecutionState.VOIDED: MilestoneStatus.VOIDED,
                ExecutionState.EXPIRED: MilestoneStatus.EXPIRED,
            }[settlement.state].value
            await self._audit(
                session, row, f"MILESTONE_{row.status.upper()}", decision="allow" if passed else "deny",
                payload=terms.canonical(), reason="; ".join(settlement.verification.reasons) if settlement.verification else None,
            )
            if row.status == MilestoneStatus.SETTLED.value and await self.is_complete(session, run_id, project_id):
                await self._audit(session, row, "PROJECT_COMPLETED", decision="allow", payload={"project_id": project_id})
        return settlement

    # ---- queries --------------------------------------------------------------------------------

    async def is_complete(self, session: AsyncSession, run_id: str, project_id: str) -> bool:
        rows = await self._rows(session, run_id, project_id)
        return bool(rows) and all(r.status == MilestoneStatus.SETTLED.value for r in rows)

    async def milestones(self, session: AsyncSession, run_id: str, project_id: str) -> list[Milestone]:
        return await self._rows(session, run_id, project_id)

    # ---- internals ------------------------------------------------------------------------------

    async def _rows(self, session: AsyncSession, run_id: str, project_id: str) -> list[Milestone]:
        stmt = (
            select(Milestone).where(Milestone.run_id == run_id, Milestone.project_id == project_id).order_by(Milestone.sequence)
        )
        return list((await session.execute(stmt)).scalars())

    async def _row(self, session: AsyncSession, run_id: str, project_id: str, sequence: int) -> Milestone:
        stmt = select(Milestone).where(
            Milestone.run_id == run_id, Milestone.project_id == project_id, Milestone.sequence == sequence
        ).with_for_update()
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise MilestoneError(f"milestone {sequence} not found")
        return row

    async def _require_prior_settled(self, session: AsyncSession, row: Milestone) -> None:
        earlier = [r for r in await self._rows(session, row.run_id, row.project_id) if r.sequence < row.sequence]
        if any(r.status != MilestoneStatus.SETTLED.value for r in earlier):
            raise MilestoneOutOfOrder(f"milestone {row.sequence} cannot start before earlier milestones are settled")

    async def _checked_terms(self, session: AsyncSession, row: Milestone) -> ApprovalTerms:
        """Terms rebuilt from the row must still hash to what was approved (detects out-of-band edits)."""
        terms = terms_for(row)
        if terms.terms_hash() != row.terms_hash:
            await self._reject(session, row.run_id, row.project_id, f"milestone {row.sequence} changed after approval", row.terms_hash)
        return terms

    async def _reject(self, session: AsyncSession, run_id: str, project_id: str, reason: str, digest: str) -> None:
        if self._audit_sink is not None:
            emit(
                self._audit_sink, run_id, Phase.DOMAIN, "MILESTONE_PLAN_REJECTED", node=_NODE, resource_type="project",
                resource_id=project_id, payload=digest, decision="deny", reason=reason,
            )
        raise MilestonePlanChanged(reason)

    async def _audit(
        self, session: AsyncSession, row: Milestone, event_type: str, *, decision: str, payload: object, reason: str | None = None
    ) -> None:
        await emit_critical(
            session, row.run_id, Phase.DOMAIN, event_type, node=_NODE, resource_type="milestone", resource_id=str(row.id),
            payload=payload, decision=decision, reason=reason,
            metadata={"project_id": row.project_id, "sequence": row.sequence, "payment_id": str(row.payment_id) if row.payment_id else None},
        )


__all__ = [
    "MilestoneError", "MilestoneOutOfOrder", "MilestonePlanChanged", "MilestoneService", "order_reference", "terms_for",
]
