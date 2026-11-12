"""Client-facing run shape (mirrors the frontend ``Run`` type) and the projection from checkpointed state.

This is the boundary where internal state is shaped for the browser. Only typed, trusted fields are copied:
ids, numbers, enums, dates, and the few display strings that are already length-bounded (candidate title, seller
id). Rejection reasons come from code, never from connector text; reviews and listing descriptions never pass here.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from app.db.models.milestone import Milestone
from app.db.models.payment import Payment
from app.db.models.run import RunRecord
from app.db.models.run_message import RunMessage
from app.domains.contractor.schemas import MilestonePlan
from app.orchestrator.graph.runtime import RunSnapshot
from app.orchestrator.router.limits import GraphLimits
from app.schemas.approval_terms import from_minor_units
from app.schemas.authorization import PendingApproval
from app.schemas.candidate import Candidate
from app.schemas.evaluator import EvaluationReport
from app.schemas.state import TaskState

RunStatus = Literal[
    "running", "awaiting_input", "awaiting_approval", "awaiting_payment", "completed", "failed", "stopped"
]
_CLIENT_STATUSES: frozenset[str] = frozenset(RunStatus.__args__)  # type: ignore[attr-defined]
_PAUSED_AT = {"await_approval": "awaiting_approval", "await_payer": "awaiting_payment", "ask_user": "awaiting_input", "choose_candidate": "awaiting_input"}
QUESTION_NODES = frozenset({"ask_user", "choose_candidate"})  # paused for a free-text answer from the user
_GRAPH_STATUS = {
    "failed": "failed", "stopped": "stopped", "cancelled": "stopped", "settled": "completed", "completed": "completed",
    "voided": "stopped", "expired": "stopped",
}
TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped"})


class PlanStepView(BaseModel):
    id: str
    title: str
    agent: str
    status: str
    summary: str | None = None


class ApprovalView(BaseModel):
    run_id: str
    item: str
    price_usd: float  # the amount in the run's currency; the name is the frontend contract
    seller: str
    delivery_by: str
    terms_hash: str


class CandidateView(BaseModel):
    id: str
    title: str
    seller: str
    price_usd: float
    rating: float
    review_count: int
    delivery_by: str
    in_stock: bool
    product_url: str | None = None
    score: float
    verdict: Literal["viable", "rejected"]
    rejection_reason: str | None = None


class MilestoneView(BaseModel):
    sequence: int
    description: str  # from the approved plan; bounded (512) and shown as plain text
    amount: float
    currency: str
    due_by: str
    status: str  # proposed | pending | order_placed | settled | voided | expired


class MessageView(BaseModel):
    id: str
    role: str
    agent: str | None = None
    text: str
    created_at: datetime
    client_id: str | None = None
    is_question: bool = False


class PaymentView(BaseModel):
    status: str
    amount: float
    currency: str
    order_id: str
    approval_url: str | None = None
    updated_at: datetime


class RunView(BaseModel):
    id: str
    goal: str
    domain: str = "shopping"
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    budget_usd: float  # the budget in the run's currency; the name is the frontend contract
    currency: str
    iteration: int = 0
    max_iterations: int = 0
    pending_approval: ApprovalView | None = None
    candidates: list[CandidateView] | None = None
    messages: list[MessageView] = []
    payment: PaymentView | None = None
    stop_reason: str | None = None
    steps: list[PlanStepView] = []
    promised_by: datetime | None = None  # the delivery date the user set / approved
    milestones: list[MilestoneView] | None = None  # contractor runs: the plan, with each milestone's progress
    expected_by: datetime | None = None  # the counterparty's latest revised date, when it slipped


class RunPageView(BaseModel):
    items: list[RunView]
    next_cursor: str | None = None


def client_status(status: str) -> RunStatus:
    """Map a stored/graph status onto the set the UI understands; unknown means still working."""
    if status in _CLIENT_STATUSES:
        return status  # type: ignore[return-value]
    return _GRAPH_STATUS.get(status, "running")  # type: ignore[return-value]


def derive_status(snapshot: RunSnapshot) -> RunStatus:
    """Where the run is: paused for a person (by the node it waits at), finished, or working."""
    for node in snapshot.next:
        if node in _PAUSED_AT:
            return _PAUSED_AT[node]  # type: ignore[return-value]
    return client_status(snapshot.values.get("status", ""))


def question_text(snapshot: RunSnapshot) -> str | None:
    """What the run is asking the user, when paused at ``ask_user``: the interrupt's message, else the intent's
    clarifying questions."""
    if not QUESTION_NODES & set(snapshot.next):
        return None
    for payload in snapshot.interrupts:
        if isinstance(payload, dict) and isinstance(payload.get("message"), str) and payload["message"].strip():
            return payload["message"].strip()
    questions = snapshot.values.get("questions") or []
    return "\n".join(questions) if questions else None


def _day(moment: datetime | None) -> str:
    return moment.astimezone(UTC).date().isoformat() if moment else ""


def approval_view(run_id: str, pending: PendingApproval) -> ApprovalView:
    terms = pending.terms
    total = terms.expected_total_minor if terms.expected_total_minor is not None else terms.max_total_minor
    return ApprovalView(
        run_id=run_id, item=terms.display.get("title", terms.subject_id), price_usd=float(from_minor_units(total, terms.currency)),
        seller=terms.display.get("seller", pending.platform), delivery_by=_day(terms.latest_acceptable_at),
        terms_hash=pending.terms_hash,
    )


def candidate_views(candidates: Sequence[Candidate], report: EvaluationReport | None) -> list[CandidateView] | None:
    """Candidates with the Evaluator's verdicts. None until an evaluation exists (nothing to judge yet)."""
    if report is None:
        return None
    scores = {r.candidate_id: r.score for r in report.ranked}
    reasons = {r.candidate_id: r.reason for r in report.rejected}
    views = []
    for c in candidates:
        viable = c.candidate_id in scores
        views.append(
            CandidateView(
                id=c.candidate_id, title=c.title, seller=c.source, price_usd=float(c.total_cost or 0), rating=c.rating or 0.0,
                review_count=c.review_count or 0, delivery_by=_day(c.available_by), in_stock=bool(c.attributes.get("in_stock", True)),
                product_url=c.product_url,
                score=round(scores.get(c.candidate_id, 0.0), 1), verdict="viable" if viable else "rejected",
                rejection_reason=None if viable else reasons.get(c.candidate_id, "not viable"),
            )
        )
    return sorted(views, key=lambda v: (v.verdict != "viable", -v.score))


def milestone_views(rows: Sequence[Milestone], plan: MilestonePlan | None) -> list[MilestoneView] | None:
    """Stored milestones once the plan is approved; before that, the proposed plan. None when there is neither."""
    if rows:
        return [
            MilestoneView(
                sequence=r.sequence, description=r.description, amount=float(r.amount), currency=r.currency,
                due_by=_day(r.due_date if r.due_date.tzinfo else r.due_date.replace(tzinfo=UTC)), status=r.status,
            )
            for r in sorted(rows, key=lambda r: r.sequence)
        ]
    if plan is None:
        return None
    return [
        MilestoneView(
            sequence=index, description=m.description, amount=float(m.amount), currency=plan.currency, due_by=_day(m.due_date),
            status="proposed",
        )
        for index, m in enumerate(plan.milestones, start=1)
    ]


def payment_view(payment: Payment, approval_url: str | None) -> PaymentView:
    return PaymentView(
        status=payment.status, amount=float(payment.amount), currency=payment.currency, order_id=payment.order_id,
        approval_url=approval_url if payment.status == "created" else None, updated_at=payment.updated_at,
    )


def message_view(row: RunMessage) -> MessageView:
    return MessageView(
        id=row.id, role=row.role, agent=row.agent, text=row.text, created_at=row.created_at, client_id=row.client_id,
        is_question=row.is_question,
    )


def plan_steps(state: TaskState, status: str) -> list[PlanStepView]:
    """The pipeline as progress, derived only from which state fields exist."""
    ended = status in TERMINAL_STATUSES
    report = state.get("evaluation")
    found = len(state.get("candidates", []))
    stages: list[tuple[str, str, str, bool, str | None]] = [
        ("intent", "Understand the request", "intent", bool(state.get("goal")), state.get("goal")),
        ("discovery", "Search stores", "discovery", found > 0, f"{found} candidates found" if found else None),
        ("evaluation", "Rank candidates", "evaluator", report is not None, report.reason if report else None),
        ("approval", "Your approval", "executor", bool(state.get("approvals")), None),
        ("payment", "Pay and verify", "payment", state.get("status") in {"settled", "completed"} or status == "completed", None),
    ]
    steps, current_taken = [], False
    for step_id, title, agent, done, summary in stages:
        if done:
            step_status = "done"
        elif current_taken:
            step_status = "pending"
        else:  # the first unfinished stage is where the run is (or where it stopped)
            current_taken = True
            step_status = "pending" if status == "completed" else ("failed" if ended else "running")
        steps.append(PlanStepView(id=step_id, title=title, agent=agent, status=step_status, summary=summary))
    return steps


def run_view(
    record: RunRecord,
    snapshot: RunSnapshot | None = None,
    *,
    payment: Payment | None = None,
    messages: Sequence[RunMessage] = (),
    max_cycles: int = GraphLimits().max_cycles,
    expected_by: datetime | None = None,
    milestones: Sequence[Milestone] = (),
) -> RunView:
    """The run as the client sees it. Without a snapshot (graph not checkpointed yet) only the index row is known."""
    base = dict(
        id=record.run_id, goal=record.goal, domain=record.domain or "shopping", created_at=record.created_at, updated_at=record.updated_at,
        budget_usd=float(record.max_spend), currency=record.currency, messages=[message_view(m) for m in messages],
        promised_by=record.deliver_by, expected_by=expected_by,
    )
    if snapshot is None:
        return RunView(
            status=client_status(record.status),
            payment=payment_view(payment, None) if payment else None, **base,
        )
    state = snapshot.values
    status = derive_status(snapshot)
    pending = state.get("pending_approval")
    if pending is not None:
        base["promised_by"] = pending.terms.latest_acceptable_at or record.deliver_by
    return RunView(
        status=status, iteration=state.get("cycles", 0), max_iterations=max_cycles,
        pending_approval=approval_view(record.run_id, pending) if pending and status == "awaiting_approval" else None,
        candidates=candidate_views(state.get("candidates", []), state.get("evaluation")),
        payment=payment_view(payment, state.get("approval_url")) if payment else None,
        stop_reason=state.get("stop_reason") if status in {"stopped", "failed"} else None,
        steps=plan_steps(state, status), milestones=milestone_views(milestones, state.get("milestone_plan")), **base,
    )
