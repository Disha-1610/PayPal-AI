"""Milestone nodes for the task graph (contractor engagements). Pure steps over state; ``builder`` wires them.

    guard_execute -> propose_plan -> recheck -> request_approval (the whole plan) -> await_approval
        -> guard_plan -> milestone_create -> milestone_next
    milestone_next -> request_approval (this milestone) -> await_approval -> guard_pay -> milestone_pay
        -> await_payer -> settle -> milestone_next ...      (all settled -> completed)

The rules are ``MilestoneService``'s: the plan is stored only if it hashes to the plan the user approved; each
milestone has its own approval and payment and starts only when every earlier one is settled; capture happens
only on a verifier PASS. A voided or expired milestone ends the run: earlier captures stand and no later
milestone is started (recovering means a new approved plan, never an amendment).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditScope, Phase
from app.db.models.milestone import Milestone, MilestoneStatus
from app.db.session import session_scope
from app.domains.contractor.milestone import (
    MilestoneError,
    MilestonePlanChanged,
    MilestoneService,
    order_reference,
    terms_for,
)
from app.domains.contractor.schemas import MilestonePlan
from app.orchestrator.executor.executor import ExecutionBlocked
from app.payments.provider import ProviderError
from app.payments.service import PaymentNotReady, PaymentPermit, PaymentServiceError
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, PendingApproval
from app.schemas.candidate import Candidate
from app.schemas.state import TaskState

STATUS_PLAN_PROPOSED = "plan_proposed"
STATUS_PLAN_CREATED = "plan_created"
STATUS_MILESTONE_READY = "milestone_ready"
STATUS_MILESTONE_FAILED = "milestone_failed"
STATUS_COMPLETED = "completed"

PermitFor = Callable[[TaskState, ApprovalTerms], PaymentPermit]


class MilestonePlanner(Protocol):
    """The contractor's proposed plan for a candidate: milestones, amounts, due dates and acceptance criteria."""

    async def propose(self, candidate: Candidate, state: TaskState) -> MilestonePlan: ...


@dataclass(frozen=True)
class MilestoneFlow:
    planner: MilestonePlanner
    service: MilestoneService


def _plan(state: TaskState) -> MilestonePlan:
    plan = state.get("milestone_plan")
    if plan is None:
        raise ValueError("no milestone plan in state")
    return plan


async def propose_plan_step(
    state: TaskState, candidate: Candidate, *, flow: MilestoneFlow, audit: AuditScope | None
) -> dict[str, object]:
    try:
        plan = await flow.planner.propose(candidate, state)
    except ValueError as exc:  # the contractor cannot offer a valid plan (budget, deadline, unknown work)
        return {"status": "plan_unavailable", "errors": [f"no milestone plan: {exc}"]}
    if audit:
        audit.emit(
            Phase.DOMAIN, "MILESTONE_PLAN_PROPOSED", node="milestones", resource_type="project", resource_id=plan.project_id,
            decision="proposed", metadata={"milestones": len(plan.milestones), "plan_hash": plan.plan_hash()},
        )
    return {"milestone_plan": plan, "milestone_seq": 0, "status": STATUS_PLAN_PROPOSED}


async def create_plan_step(
    state: TaskState, *, flow: MilestoneFlow, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, object]:
    """Persist the plan the user just approved. The approved plan hash comes from the signed terms, never from state."""
    approved = state["approvals"][-1].terms
    plan = _plan(state)
    approved_hash = str(approved.domain_data.get("plan_hash", "")) if approved else ""
    try:
        async with session_scope(session_factory) as session:
            await flow.service.create_plan(session, run_id=state["run_id"], plan=plan, approved_plan_hash=approved_hash)
    except MilestonePlanChanged as exc:
        return {"status": "plan_rejected", "errors": [f"milestone plan refused: {exc}"]}
    return {"status": STATUS_PLAN_CREATED}


async def _rows(flow: MilestoneFlow, session_factory: async_sessionmaker[AsyncSession], state: TaskState) -> list[Milestone]:
    async with session_factory() as session:
        return await flow.service.milestones(session, state["run_id"], _plan(state).project_id)


def milestone_terms(row: Milestone, total: int, title: str) -> ApprovalTerms:
    """The milestone's signed terms plus presentation text (``display`` is not hashed)."""
    return terms_for(row).model_copy(
        update={"display": {"title": f"Milestone {row.sequence} of {total}: {row.description}", "seller": title}}
    )


async def next_milestone_step(
    state: TaskState, candidate: Candidate, *, flow: MilestoneFlow, session_factory: async_sessionmaker[AsyncSession],
    audit: AuditScope | None,
) -> dict[str, object]:
    """Put the next milestone to the user, finish when every milestone is settled, or stop on a failed one."""
    rows = await _rows(flow, session_factory, state)
    if not rows:
        return {"status": STATUS_MILESTONE_FAILED, "errors": ["the approved plan has no stored milestones"]}
    failed = next((r for r in rows if r.status in (MilestoneStatus.VOIDED.value, MilestoneStatus.EXPIRED.value)), None)
    if failed is not None:
        paid = sum(1 for r in rows if r.status == MilestoneStatus.SETTLED.value)
        return {
            "status": STATUS_MILESTONE_FAILED, "milestone_seq": 0, "agreed_terms": None,
            "errors": [
                (
                    f"milestone {failed.sequence} was {failed.status} (nothing captured for it); {paid} of {len(rows)} "
                    "milestones were paid and the remaining ones were not started"
                )
            ],
        }
    upcoming = next((r for r in rows if r.status != MilestoneStatus.SETTLED.value), None)
    if upcoming is None:
        if audit:
            audit.emit(Phase.DOMAIN, "MILESTONES_COMPLETED", node="milestones", decision="completed", metadata={"milestones": len(rows)})
        return {"status": STATUS_COMPLETED, "milestone_seq": 0, "agreed_terms": None}
    if upcoming.status != MilestoneStatus.PENDING.value:  # an order is already open: never start it twice
        return {"status": STATUS_MILESTONE_FAILED, "errors": [f"milestone {upcoming.sequence} is {upcoming.status}, expected pending"]}
    terms = milestone_terms(upcoming, len(rows), candidate.title)
    return {
        "milestone_seq": upcoming.sequence, "agreed_terms": terms, "status": STATUS_MILESTONE_READY,
        "pending_approval": PendingApproval(
            action=Action.CREATE_ORDER, candidate_id=candidate.candidate_id, platform=candidate.source, terms=terms,
            terms_hash=terms.terms_hash(),
        ),
    }


async def pay_milestone_step(
    state: TaskState, *, flow: MilestoneFlow, session_factory: async_sessionmaker[AsyncSession], permit_for: PermitFor
) -> dict[str, object]:
    """Open this milestone's payment for the approved amount (the payer then approves it at PayPal)."""
    terms = state.get("agreed_terms")
    seq = state.get("milestone_seq", 0)
    if terms is None or not seq:
        return {"status": STATUS_MILESTONE_FAILED, "errors": ["no approved milestone to pay"]}
    permit = permit_for(state, terms)
    approval = next(a for a in permit.approvals if a.action is Action.CREATE_ORDER)
    project_id = _plan(state).project_id
    try:
        async with session_scope(session_factory) as session:
            try:
                placed = await flow.service.start(
                    session, run_id=state["run_id"], project_id=project_id, sequence=seq, permit=permit, approval=approval
                )
            except ExecutionBlocked as blocked:  # commit the recheck audit row before reporting it
                return {
                    "recheck_report": blocked.report, "status": "milestone_blocked",
                    "errors": [f"milestone {seq} terms no longer hold: {blocked.report.action.name}"],
                }
            row = next(r for r in await flow.service.milestones(session, state["run_id"], project_id) if r.sequence == seq)
            order_id = order_reference(row)
    except MilestoneError as exc:
        return {"status": STATUS_MILESTONE_FAILED, "errors": [f"milestone {seq} refused: {exc}"]}
    except PaymentServiceError as exc:
        return {"status": "payment_refused", "errors": [f"payment refused: {exc}"]}
    except ProviderError as exc:
        return {"status": "payment_refused", "errors": [f"payment provider error: {exc}"]}
    return {
        "order_id": order_id, "payment_id": str(placed.created.payment.id), "approval_url": placed.created.approval_url,
        "settle_attempt": 0, "status": "awaiting_payer",
    }


async def settle_milestone_step(
    state: TaskState, *, flow: MilestoneFlow, session_factory: async_sessionmaker[AsyncSession], permit_for: PermitFor
) -> dict[str, object]:
    """Verify the delivered milestone, then capture (PASS) or void (FAIL)."""
    terms = state.get("agreed_terms")
    seq = state.get("milestone_seq", 0)
    if terms is None or not seq:
        return {"status": STATUS_MILESTONE_FAILED, "errors": ["no milestone payment to settle"]}
    try:
        async with session_scope(session_factory) as session:
            settlement = await flow.service.settle(
                session, run_id=state["run_id"], project_id=_plan(state).project_id, sequence=seq, permit=permit_for(state, terms)
            )
    except PaymentNotReady as exc:
        return {"status": "payer_pending", "errors": [str(exc)]}
    except PaymentServiceError as exc:
        return {"status": "payment_refused", "errors": [str(exc)]}
    except ProviderError as exc:
        return {"status": "payer_pending" if exc.uncertain else "payment_refused", "errors": [f"payment provider error: {exc}"]}
    except MilestoneError as exc:
        return {"status": STATUS_MILESTONE_FAILED, "errors": [f"milestone {seq}: {exc}"]}
    return {"settle_attempt": state.get("settle_attempt", 0) + 1, "status": settlement.state.value}


__all__ = [
    "STATUS_COMPLETED",
    "STATUS_MILESTONE_FAILED",
    "STATUS_MILESTONE_READY",
    "STATUS_PLAN_CREATED",
    "STATUS_PLAN_PROPOSED",
    "MilestoneFlow",
    "MilestonePlanner",
    "create_plan_step",
    "milestone_terms",
    "next_milestone_step",
    "pay_milestone_step",
    "propose_plan_step",
    "settle_milestone_step",
]
