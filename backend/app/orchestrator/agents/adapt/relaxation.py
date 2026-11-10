"""Phase 4.3 - minimum-change relaxation proposals, computed from the cached candidate pool.

No connector calls: the pool in ``TaskState.candidates`` already holds every candidate that failed
only narrowly. For each hard constraint we find the smallest edit that admits the candidates that
fail *only that constraint*, so the user is asked for one precise, cheap concession.

Domain-agnostic: edits depend on the constraint's operator, not on what the field means.
  * range (gt/gte/lt/lte) : move the bound to the candidate's actual value
  * ==  / in (scalar)     : allow the candidate's value as well
  * in (list) / not_in    : drop the wanted / excluded items that block the candidate
  * != / boolean ==       : drop the requirement
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.orchestrator.agents.evaluation.diagnosis import Violation, diagnose
from app.orchestrator.agents.evaluation.constraint_eval import normalize, to_decimal
from app.schemas.adapt import (
    AdaptPolicy,
    RelaxationOp,
    RelaxationProposal,
    UserDecision,
)
from app.schemas.candidate import Candidate
from app.schemas.constraints import (
    Constraint,
    ConstraintDiff,
    ConstraintSource,
    ConstraintValue,
    Operator,
    diff_constraints,
)
from app.schemas.intent import AppliedChange, PatchOp


class ProposalError(ValueError):
    """The accepted proposal is unknown or no longer matches the constraint it was computed for."""


@dataclass(frozen=True, slots=True)
class _Edit:
    operator: Operator | None  # None: remove the constraint
    value: ConstraintValue | None
    unlocked: frozenset[str]
    cost: float


# --------------------------------------------------------------------------- per-operator edits


def _range_edits(constraint: Constraint, violations: list[Violation]) -> list[_Edit]:
    target = to_decimal(constraint.target_value)
    scale = max(abs(target), Decimal(1))
    new_operator = Operator.LTE if constraint.operator in (Operator.LT, Operator.LTE) else Operator.GTE
    rows = sorted(((abs(to_decimal(v.actual) - target), to_decimal(v.actual), v.candidate_id) for v in violations))

    edits: list[_Edit] = []
    unlocked: set[str] = set()
    for index, (distance, actual, candidate_id) in enumerate(rows):
        unlocked.add(candidate_id)
        if index + 1 == len(rows) or rows[index + 1][1] != actual:
            edits.append(_Edit(new_operator, actual, frozenset(unlocked), float(distance / scale)))
    return edits


def _allow_value_edits(constraint: Constraint, violations: list[Violation]) -> list[_Edit]:
    """== / in against a scalar attribute: widen to ``in`` with the missed value(s)."""
    current = list(constraint.target_value) if isinstance(constraint.target_value, list) else [constraint.target_value]
    by_value: dict[object, tuple[object, set[str]]] = {}
    for v in violations:
        key = normalize(v.actual)
        raw, ids = by_value.setdefault(key, (v.actual, set()))
        ids.add(v.candidate_id)

    def cost(added: int) -> float:
        return added / (len(current) + added)

    edits = [
        _Edit(Operator.IN, [*current, raw], frozenset(ids), cost(1))  # type: ignore[list-item]
        for raw, ids in by_value.values()
    ]
    if len(by_value) > 1:
        everyone = frozenset(v.candidate_id for v in violations)
        edits.append(_Edit(Operator.IN, [*current, *(raw for raw, _ in by_value.values())], everyone, cost(len(by_value))))  # type: ignore[list-item]
    return edits


def _blocking_indices(constraint: Constraint, violation: Violation) -> frozenset[int]:
    """Indices of the constraint's list items that make this candidate fail."""
    items = constraint.target_value
    assert isinstance(items, list)
    actual = normalize(violation.actual)
    held = actual if isinstance(actual, list) else [actual]
    if constraint.operator is Operator.IN:
        return frozenset(i for i, item in enumerate(items) if normalize(item) not in held)
    return frozenset(i for i, item in enumerate(items) if normalize(item) in held)


def _drop_item_edits(constraint: Constraint, violations: list[Violation]) -> list[_Edit]:
    items = constraint.target_value
    assert isinstance(items, list)
    blocking = {v.candidate_id: _blocking_indices(constraint, v) for v in violations}

    edits: list[_Edit] = []
    dropped: frozenset[int] = frozenset()
    for step in sorted(set(blocking.values()), key=lambda s: (len(s), sorted(s))):
        dropped = dropped | step
        unlocked = frozenset(cid for cid, needed in blocking.items() if needed <= dropped)
        cost = len(dropped) / len(items)
        remaining = [item for i, item in enumerate(items) if i not in dropped]
        edits.append(_Edit(constraint.operator if remaining else None, remaining or None, unlocked, cost))
    return edits


def _edits_for(constraint: Constraint, violations: list[Violation]) -> list[_Edit]:
    op, target = constraint.operator, constraint.target_value
    everyone = frozenset(v.candidate_id for v in violations)
    if op in (Operator.GT, Operator.GTE, Operator.LT, Operator.LTE):
        return _range_edits(constraint, violations)
    if op is Operator.NE or (op is Operator.EQ and isinstance(target, bool)):
        return [_Edit(None, None, everyone, 1.0)]
    if op is Operator.EQ or (op is Operator.IN and not any(isinstance(v.actual, list) for v in violations)):
        return _allow_value_edits(constraint, violations)
    return _drop_item_edits(constraint, violations)


# --------------------------------------------------------------------------- proposals


def _fmt(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value)
    return str(value)


def _proposal(constraint: Constraint, edit: _Edit) -> RelaxationProposal:
    removing = edit.operator is None
    count = len(edit.unlocked)
    now = f"{constraint.operator.value} {_fmt(constraint.target_value)}"
    if removing:
        description = f"Drop the requirement on {constraint.field_name} ({now})"
    else:
        description = f"Relax {constraint.field_name} from {now} to {edit.operator.value} {_fmt(edit.value)}"  # type: ignore[union-attr]
    key = f"{constraint.constraint_id}|{constraint.revision}|{edit.operator}|{_fmt(edit.value)}"
    return RelaxationProposal(
        proposal_id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
        constraint_id=constraint.constraint_id,
        constraint_revision=constraint.revision,
        field_name=constraint.field_name,
        op=RelaxationOp.REMOVE if removing else RelaxationOp.UPDATE,
        operator=edit.operator,
        new_value=edit.value,
        unlocked_ids=tuple(sorted(edit.unlocked)),
        cost=edit.cost,
        description=f"{description}: {count} more option(s)",
    )


def propose_relaxations(
    candidates: Iterable[Candidate],
    constraints: Iterable[Constraint],
    *,
    policy: AdaptPolicy | None = None,
    excluded_ids: frozenset[str] = frozenset(),
) -> list[RelaxationProposal]:
    """Best single-constraint relaxations first: most options unlocked, then smallest change.

    ``excluded_ids`` removes candidates that relaxing could not rescue (e.g. blocked by credibility),
    so the promised option count is real. Candidates failing several constraints, or whose data is
    unverifiable, never count: no single relaxation is shown to admit them.
    """
    policy = policy or AdaptPolicy()
    by_constraint: dict[str, list[Violation]] = defaultdict(list)
    constraint_by_id: dict[str, Constraint] = {}
    for diagnosis in diagnose(candidates, constraints):
        near = diagnosis.near_miss
        if near is None or diagnosis.candidate_id in excluded_ids:
            continue
        by_constraint[near.constraint.constraint_id].append(near)
        constraint_by_id[near.constraint.constraint_id] = near.constraint

    proposals = [
        _proposal(constraint_by_id[cid], edit)
        for cid, violations in by_constraint.items()
        for edit in _edits_for(constraint_by_id[cid], violations)
    ]
    ranked = sorted(proposals, key=lambda p: (-len(p.unlocked_ids), p.cost, p.field_name, p.proposal_id))
    return ranked[: policy.max_proposals]


# --------------------------------------------------------------------------- applying an accepted proposal


def apply_proposal(
    decision: UserDecision, proposals: Sequence[RelaxationProposal], constraints: Sequence[Constraint]
) -> tuple[list[Constraint], ConstraintDiff, AppliedChange]:
    """Apply the proposal the user accepted, as a user-sourced revision.

    Call only with a ``UserDecision`` parsed from the user's reply to ``interrupt()``: that is what
    makes ``changed_by=USER`` true. The state reducer's guard still re-checks the resulting revision.
    """
    proposal = next((p for p in proposals if p.proposal_id == decision.proposal_id), None)
    if proposal is None:
        raise ProposalError(f"unknown proposal {decision.proposal_id!r}")
    target = next((c for c in constraints if c.constraint_id == proposal.constraint_id), None)
    if target is None or not target.active or target.revision != proposal.constraint_revision:
        raise ProposalError(f"proposal {proposal.proposal_id} is out of date: the constraint has changed")

    removing = proposal.op is RelaxationOp.REMOVE
    revised = target.revise(
        reason=f"user accepted relaxation: {proposal.description}",
        changed_by=ConstraintSource.USER,
        operator=proposal.operator,
        target_value=proposal.new_value,
        active=False if removing else None,
    )
    updated = [revised if c.constraint_id == revised.constraint_id else c for c in constraints]
    change = AppliedChange(
        op=PatchOp.REMOVE if removing else PatchOp.UPDATE,
        field=target.field_name,
        old=target.target_value,
        new=None if removing else revised.target_value,
        source=ConstraintSource.USER,
    )
    return updated, diff_constraints(list(constraints), updated), change
