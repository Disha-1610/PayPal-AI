"""Step 3.3 - weighted ranking. Deterministic: the LLM may only supply the weight vector.

Criteria are ``trust`` (from 3.2) plus one per active SOFT constraint, each scored 0-1:
1.0 when satisfied, a proportional partial score when a numeric target is narrowly missed,
0.0 when it fails otherwise or cannot be verified (missing data is never rewarded).
Candidates blocked by credibility are excluded. Ties break on trust, then cost, then id.
"""

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

from app.orchestrator.agents.evaluation.constraint_eval import (
    Unverifiable,
    resolve_value,
    satisfies,
    to_decimal,
)
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintStrength, Operator
from app.schemas.evaluation import TRUST_CRITERION, CredibilityAssessment, RankedCandidate, RankingResult

_AT_LEAST = frozenset({Operator.GT, Operator.GTE})
_AT_MOST = frozenset({Operator.LT, Operator.LTE})


def _partial_numeric(constraint: Constraint, actual: object) -> float:
    """Score a missed numeric range target in [0, 1): how close the value came."""
    try:
        value, target = to_decimal(actual), to_decimal(constraint.target_value)
    except Unverifiable:
        return 0.0
    if value <= 0 or target <= 0:
        return 0.0
    ratio = value / target if constraint.operator in _AT_LEAST else target / value
    return float(min(ratio, Decimal(1)))


def _criterion_score(candidate: Candidate, constraint: Constraint) -> float:
    try:
        actual = resolve_value(candidate, constraint.field_name)
        if satisfies(constraint, actual):
            return 1.0
    except Unverifiable:
        return 0.0
    if constraint.operator in _AT_LEAST | _AT_MOST:
        return round(_partial_numeric(constraint, actual), 4)
    return 0.0


def normalise_weights(weights: Mapping[str, float] | None, criteria: Sequence[str]) -> dict[str, float]:
    """Validate and normalise weights to sum to 1. Unspecified criteria default to 1.0 (equal importance)."""
    given = dict(weights or {})
    unknown = set(given) - set(criteria)
    if unknown:
        raise ValueError(f"weights for unknown criteria: {sorted(unknown)}")
    if not all(math.isfinite(w) for w in given.values()):
        raise ValueError("weights must be finite numbers")
    if any(w < 0 for w in given.values()):
        raise ValueError("weights must be non-negative")
    raw = {name: given.get(name, 1.0) for name in criteria}
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("at least one weight must be positive")
    return {name: w / total for name, w in raw.items()}


def rank_candidates(
    candidates: Sequence[Candidate],
    assessments: Sequence[CredibilityAssessment],
    constraints: Sequence[Constraint],
    weights: Mapping[str, float] | None = None,
) -> RankingResult:
    trust = {a.candidate_id: a for a in assessments}
    missing = [c.candidate_id for c in candidates if c.candidate_id not in trust]
    if missing:
        raise ValueError(f"no credibility assessment for candidates: {missing}")

    soft = [c for c in constraints if c.active and c.strength is ConstraintStrength.SOFT]
    names = [c.field_name for c in soft]
    if len(set(names)) != len(names):
        raise ValueError("duplicate soft constraint field names are ambiguous for weighting")
    applied = normalise_weights(weights, [TRUST_CRITERION, *names])

    scored: list[tuple[Candidate, RankedCandidate]] = []
    for candidate in candidates:
        if trust[candidate.candidate_id].blocked:
            continue
        breakdown = {TRUST_CRITERION: trust[candidate.candidate_id].trust_score}
        breakdown.update({c.field_name: _criterion_score(candidate, c) for c in soft})
        total = round(sum(applied[name] * value for name, value in breakdown.items()), 4)
        scored.append(
            (candidate, RankedCandidate(candidate_id=candidate.candidate_id, score=total, breakdown=breakdown))
        )

    scored.sort(
        key=lambda pair: (
            -pair[1].score,
            -pair[1].breakdown[TRUST_CRITERION],
            pair[0].total_cost if pair[0].total_cost is not None else Decimal("Infinity"),
            pair[0].candidate_id,
        )
    )
    ranked = tuple(r for _, r in scored)
    margin = round(ranked[0].score - ranked[1].score, 4) if len(ranked) >= 2 else None
    return RankingResult(ranked=ranked, weights=applied, margin=margin)
