"""TaskState: the central contract shared by every lifecycle node.

Nodes read the state and return a *partial* update. Rules enforced here:
  * Every node uses this one definition.
  * Reducers are used only where aggregation/protection is needed; all other
    fields are last-write-wins and written only by their owning node.
  * ``original_intent`` and ``authorization`` are write-once: replanning cannot overwrite them.
  * Constraint updates are upserts by id, must advance the revision, and must keep history.
"""

from datetime import datetime
from operator import add
from typing import Annotated, TypedDict

from app.orchestrator.guard.constraint_guard import assert_revision_allowed
from app.domains.contractor.schemas import MilestonePlan
from app.schemas.adapt import AdaptProgress, RelaxationProposal
from app.schemas.authorization import Approval, PendingApproval, TaskAuthorization
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintDiff, diff_constraints
from app.schemas.coordinator import CoordinatorProgress
from app.schemas.discovery import DiscoveryProgress
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.evaluator import EvaluationReport
from app.schemas.negotiator import CounterOffer, NegotiationResult
from app.schemas.recheck import MismatchReport

STATE_VERSION = 1


def keep_first[T](old: T | None, new: T | None) -> T | None:
    """Write-once reducer: the first set value wins; later writes are ignored.

    LangGraph seeds reducer channels with the type's empty value (``""``/``0``), so those
    count as unset alongside ``None``.
    """
    unset = old is None or (isinstance(old, (str, int)) and not old)
    return new if unset else old


def merge_constraints(old: list[Constraint] | None, new: list[Constraint] | None) -> list[Constraint]:
    """Upsert constraints by id while protecting revision history and user intent."""
    merged = {c.constraint_id: c for c in old or []}
    for incoming in new or []:
        existing = merged.get(incoming.constraint_id)
        if existing is None or incoming == existing:
            merged[incoming.constraint_id] = incoming
            continue
        if incoming.revision != existing.revision + 1:
            raise ValueError(
                f"constraint {incoming.constraint_id}: revision {incoming.revision} "
                f"does not follow {existing.revision}"
            )
        if incoming.history[:-1] != existing.history:
            raise ValueError(f"constraint {incoming.constraint_id}: revision history was not preserved")
        assert_revision_allowed(existing, incoming)
        merged[incoming.constraint_id] = incoming
    return list(merged.values())


class TaskState(TypedDict, total=False):
    # --- identity / contract (write-once) ---
    state_version: Annotated[int, keep_first]
    run_id: Annotated[str, keep_first]
    original_intent: Annotated[str, keep_first]
    authorization: Annotated[TaskAuthorization, keep_first]
    deliver_by: Annotated[datetime | None, keep_first]  # hard deadline the user set at intake (negotiating domains)

    # --- aggregated history ---
    constraints: Annotated[list[Constraint], merge_constraints]
    constraint_diffs: Annotated[list[ConstraintDiff], add]
    approvals: Annotated[list[Approval], add]
    errors: Annotated[list[str], add]
    cycles: Annotated[int, add]  # entries into the search loop; each node adds 1, never overwrites
    cycle_hashes: Annotated[list[str], add]  # progress hash at each entry; a repeat means no progress

    # --- single-owner fields (last write wins) ---
    status: str
    goal: str
    questions: list[str]
    plan: dict[str, object]
    candidates: list[Candidate]
    adapt: AdaptProgress
    coordinator: CoordinatorProgress
    discovery: DiscoveryProgress
    evaluation: EvaluationReport
    relaxation_proposals: list[RelaxationProposal]
    selected_candidate_id: str
    declined_candidate_ids: list[str]  # products the user declined at approval; excluded from the next pick
    order_id: str
    domain: str  # domain plug-in name; the Monitor loads its spec from this
    anchor_at: datetime  # when the order/engagement started; turns "within N days" into a date
    pending_approval: PendingApproval | None  # terms awaiting the user's decision; cleared once answered
    negotiation_round: int  # negotiation rounds played; the Negotiator's round cap reads it
    incoming_offer: CounterOffer | None  # the counterparty's sanitized offer awaiting the Negotiator
    negotiation_result: NegotiationResult  # the Negotiator's last result (the Router reads it)
    agreed_terms: ApprovalTerms | None  # terms a negotiation put to the user; the payment Guard checks the approval against them
    recheck_report: MismatchReport  # the Executor's last recheck; the Router reads it
    payment_id: str  # ledger payment of the order in flight (a UUID string)
    approval_url: str | None  # where the payer approves at the provider
    settle_attempt: int  # verification attempts so far
    revision: str  # the user's requested change to the requirements, awaiting the ``revise`` node
    stop_reason: str  # why the task ended early (set by stop_report)
    milestone_plan: MilestonePlan | None  # the contractor's proposed plan; stored only once the user approves its hash
    milestone_seq: int  # the milestone in flight (1-based); 0 = the plan itself / none


STATE_FIELDS = frozenset(TaskState.__annotations__)


def make_initial_state(
    *,
    run_id: str,
    original_intent: str,
    authorization: TaskAuthorization,
    constraints: list[Constraint] | None = None,
    domain: str | None = None,
    deliver_by: datetime | None = None,
) -> TaskState:
    if authorization.run_id != run_id:
        raise ValueError("authorization.run_id does not match run_id")
    initial = constraints or []
    state = TaskState(
        state_version=STATE_VERSION,
        run_id=run_id,
        original_intent=original_intent,
        authorization=authorization,
        constraints=initial,
        constraint_diffs=[diff_constraints([], initial)] if initial else [],
        approvals=[],
        errors=[],
        status="created",
    )
    if domain is not None:
        state["domain"] = domain
    if deliver_by is not None:
        state["deliver_by"] = deliver_by
    return state


def validate_state_update(node: str, update: dict[str, object], writable: frozenset[str]) -> None:
    """Reject a node's partial update that touches unknown or non-owned fields."""
    unknown = set(update) - STATE_FIELDS
    if unknown:
        raise ValueError(f"node {node!r} returned unknown state fields: {sorted(unknown)}")
    forbidden = set(update) - writable
    if forbidden:
        raise ValueError(f"node {node!r} may not write state fields: {sorted(forbidden)}")
