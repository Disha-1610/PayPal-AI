"""Step 3.4 - outcome classifier. A deterministic rule cascade over the 3.1-3.3 results.

Decides whether the agent found something usable or must go back to discovery (Phase 4).
Retries are capped by ``attempt``; the caller increments it on every re-search.
"""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.evaluation import (
    CredibilityAssessment,
    EvaluationOutcome,
    HardFilterResult,
    OutcomeKind,
    RankingResult,
)


class OutcomePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_score: float = Field(default=0.5, ge=0, le=1, description="Best score below this is not a usable result")
    min_margin: float = Field(default=0.05, ge=0, le=1, description="Top-two gap below this is a near-tie")
    max_retries: int = Field(default=2, ge=0)


def _most_restrictive_constraint(filtered: HardFilterResult) -> str | None:
    counts = filtered.eliminations_by_constraint()
    return min(counts, key=lambda cid: (-counts[cid], cid)) if counts else None


def _retry_or_stop(
    reason: str, filtered: HardFilterResult, attempt: int, policy: OutcomePolicy
) -> EvaluationOutcome:
    if attempt >= policy.max_retries:
        return EvaluationOutcome(kind=OutcomeKind.NO_RESULT, reason=f"{reason}; retries exhausted")
    return EvaluationOutcome(
        kind=OutcomeKind.RETRY_SEARCH,
        reason=reason,
        relax_constraint_id=_most_restrictive_constraint(filtered),
    )


def classify_outcome(
    filtered: HardFilterResult,
    assessments: Sequence[CredibilityAssessment],
    ranking: RankingResult,
    *,
    attempt: int = 0,
    policy: OutcomePolicy | None = None,
) -> EvaluationOutcome:
    policy = policy or OutcomePolicy()
    total = len(filtered.survivors) + len(filtered.failed_ids | filtered.unverifiable_ids)

    if total == 0:
        return _retry_or_stop("discovery returned no candidates", filtered, attempt, policy)

    if not ranking.ranked:
        if filtered.survivors:
            return _retry_or_stop("every candidate that passed the hard filter failed credibility", filtered, attempt, policy)
        if filtered.unverifiable_ids:
            return EvaluationOutcome(
                kind=OutcomeKind.NEEDS_USER_INPUT,
                reason="no candidate could be fully verified against the hard constraints",
                options=tuple(sorted(filtered.unverifiable_ids)),
            )
        return _retry_or_stop("no candidate satisfies the hard constraints", filtered, attempt, policy)

    top = ranking.ranked[0]
    if top.score < policy.min_score:
        reason = f"best candidate scored {top.score:.2f}, below the usable threshold {policy.min_score:.2f}"
        if attempt >= policy.max_retries or _most_restrictive_constraint(filtered) is None:
            # Every ranked candidate meets the hard requirements; a repeat search with nothing to relax returns the
            # same pool. Let the user choose rather than loop until the run stops.
            return EvaluationOutcome(
                kind=OutcomeKind.NEEDS_USER_INPUT, reason=reason, options=tuple(r.candidate_id for r in ranking.ranked)
            )
        return _retry_or_stop(reason, filtered, attempt, policy)

    if ranking.margin is not None and ranking.margin < policy.min_margin:
        tied = tuple(r.candidate_id for r in ranking.ranked if top.score - r.score < policy.min_margin)
        return EvaluationOutcome(
            kind=OutcomeKind.NEEDS_USER_INPUT,
            reason=f"top candidates are nearly tied (margin {ranking.margin:.2f})",
            options=tied,
        )

    flags = next((a.flags for a in assessments if a.candidate_id == top.candidate_id), ())
    caveats = [f"credibility: {flag.value}" for flag in flags]
    if filtered.unverifiable_ids:
        caveats.append(
            f"{len(filtered.unverifiable_ids)} candidate(s) excluded because a hard requirement could not be verified"
        )
    return EvaluationOutcome(
        kind=OutcomeKind.RECOMMEND_WITH_CAVEATS if caveats else OutcomeKind.RECOMMEND,
        reason=f"top candidate scored {top.score:.2f}",
        selected_candidate_id=top.candidate_id,
        caveats=tuple(caveats),
    )
