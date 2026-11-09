"""Phase 4.1 - failure diagnosis: which constraint eliminated each candidate, and by how much.

Pure code over the candidate pool already held in state, so it costs no connector calls. Unlike the
hard filter's rejection list it also keeps the candidate's actual value, which relaxation needs to
compute the smallest change that would admit the candidate.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.orchestrator.agents.evaluation.constraint_eval import Unverifiable, resolve_value, satisfies
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintStrength


@dataclass(frozen=True, slots=True)
class Violation:
    candidate_id: str
    constraint: Constraint
    actual: object


@dataclass(frozen=True, slots=True)
class CandidateDiagnosis:
    candidate_id: str
    violations: tuple[Violation, ...]  # constraints the candidate verifiably fails
    unverifiable: bool  # a hard constraint could not be checked: relaxing cannot be proven to help

    @property
    def near_miss(self) -> Violation | None:
        """The single violated constraint when it is the only obstacle, else None."""
        if self.unverifiable or len(self.violations) != 1:
            return None
        return self.violations[0]


def hard_constraints(constraints: Iterable[Constraint]) -> list[Constraint]:
    return [c for c in constraints if c.active and c.strength is ConstraintStrength.HARD]


def diagnose(candidates: Iterable[Candidate], constraints: Iterable[Constraint]) -> list[CandidateDiagnosis]:
    """Check every hard constraint for every candidate (no short-circuit). Passing candidates are omitted."""
    hard = hard_constraints(constraints)
    diagnoses: list[CandidateDiagnosis] = []
    for candidate in candidates:
        violations: list[Violation] = []
        unverifiable = False
        for constraint in hard:
            try:
                actual = resolve_value(candidate, constraint.field_name)
                if not satisfies(constraint, actual):
                    violations.append(Violation(candidate.candidate_id, constraint, actual))
            except Unverifiable:
                unverifiable = True
        if violations or unverifiable:
            diagnoses.append(CandidateDiagnosis(candidate.candidate_id, tuple(violations), unverifiable))
    return diagnoses


def summarize(diagnoses: Sequence[CandidateDiagnosis]) -> str:
    """Human-readable elimination counts per field, for the termination report. Ids and counts only."""
    counts: Counter[str] = Counter(v.constraint.field_name for d in diagnoses for v in d.violations)
    unverifiable = sum(1 for d in diagnoses if d.unverifiable)
    parts = [f"{field}: {n} candidate(s)" for field, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if unverifiable:
        parts.append(f"unverifiable data: {unverifiable} candidate(s)")
    return "; ".join(parts) or "no candidates were found"
