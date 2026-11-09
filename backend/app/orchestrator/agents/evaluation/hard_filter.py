"""Step 3.1 - hard filter. Pure, deterministic code: no LLM, no I/O.

Only active HARD constraints are evaluated; soft constraints are left to ranking (3.3).
A candidate with any failed or unverifiable hard constraint is excluded. Unverifiable
(missing / unreliable / wrongly-typed data) is reported separately from failed so the
caller can offer to verify it instead of silently treating it as a pass or a fail.
"""

from collections.abc import Iterable

from app.orchestrator.agents.evaluation.constraint_eval import Unverifiable, resolve_value, satisfies
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintStrength
from app.schemas.evaluation import HardFilterResult, Rejection, RejectionKind


def _evaluate(candidate: Candidate, constraint: Constraint) -> Rejection | None:
    def reject(kind: RejectionKind, reason: str) -> Rejection:
        return Rejection(
            candidate_id=candidate.candidate_id,
            constraint_id=constraint.constraint_id,
            field_name=constraint.field_name,
            kind=kind,
            reason=reason,
        )

    try:
        actual = resolve_value(candidate, constraint.field_name)
        if satisfies(constraint, actual):
            return None
    except Unverifiable as exc:
        return reject(RejectionKind.UNVERIFIABLE, str(exc))
    return reject(
        RejectionKind.FAILED,
        f"{constraint.field_name}: {actual} does not satisfy {constraint.operator.value} {constraint.target_value}",
    )


def hard_filter(candidates: Iterable[Candidate], constraints: Iterable[Constraint]) -> HardFilterResult:
    hard = [c for c in constraints if c.active and c.strength is ConstraintStrength.HARD]
    survivors: list[Candidate] = []
    rejections: list[Rejection] = []
    for candidate in candidates:
        found = [r for c in hard if (r := _evaluate(candidate, c)) is not None]
        rejections.extend(found)
        if not found:
            survivors.append(candidate)
    return HardFilterResult(survivors=tuple(survivors), rejections=tuple(rejections))
