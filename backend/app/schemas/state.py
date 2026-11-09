"""TaskState: the central contract shared by every lifecycle node.

Nodes read the state and return a *partial* update. Rules enforced here:
  * Every node uses this one definition.
  * Reducers are used only where aggregation/protection is needed; all other
    fields are last-write-wins and written only by their owning node.
  * ``original_intent`` and ``authorization`` are write-once: replanning cannot overwrite them.
  * Constraint updates are upserts by id, must advance the revision, and must keep history.
"""

from operator import add
from typing import Annotated, TypedDict

from app.orchestrator.guard.constraint_guard import assert_revision_allowed
from app.schemas.adapt import AdaptProgress, RelaxationProposal
from app.schemas.authorization import Approval, TaskAuthorization
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintDiff, diff_constraints

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

    # --- aggregated history ---
    constraints: Annotated[list[Constraint], merge_constraints]
    constraint_diffs: Annotated[list[ConstraintDiff], add]
    approvals: Annotated[list[Approval], add]
    errors: Annotated[list[str], add]

    # --- single-owner fields (last write wins) ---
    status: str
    goal: str
    questions: list[str]
    plan: dict[str, object]
    candidates: list[Candidate]
    adapt: AdaptProgress
    relaxation_proposals: list[RelaxationProposal]
    selected_candidate_id: str
    order_id: str


STATE_FIELDS = frozenset(TaskState.__annotations__)


def make_initial_state(
    *,
    run_id: str,
    original_intent: str,
    authorization: TaskAuthorization,
    constraints: list[Constraint] | None = None,
) -> TaskState:
    if authorization.run_id != run_id:
        raise ValueError("authorization.run_id does not match run_id")
    initial = constraints or []
    return TaskState(
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


def validate_state_update(node: str, update: dict[str, object], writable: frozenset[str]) -> None:
    """Reject a node's partial update that touches unknown or non-owned fields."""
    unknown = set(update) - STATE_FIELDS
    if unknown:
        raise ValueError(f"node {node!r} returned unknown state fields: {sorted(unknown)}")
    forbidden = set(update) - writable
    if forbidden:
        raise ValueError(f"node {node!r} may not write state fields: {sorted(forbidden)}")
