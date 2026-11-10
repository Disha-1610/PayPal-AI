"""Blend the deterministic score with the optional LLM score. The LLM can only lower, never promote."""

from collections.abc import Mapping, Sequence
from decimal import Decimal

from app.schemas.candidate import Candidate
from app.schemas.evaluation import CredibilityAssessment, RankedCandidate, RankingResult
from app.schemas.evaluator import CandidateScore, RankedViable


def blend_score(code_score: float, llm: CandidateScore | None, *, flagged: bool, weight: float) -> float:
    """``(1 - w) * code + w * llm``. A candidate carrying any code or LLM flag is capped at its code score."""
    if llm is None:
        return code_score
    blended = (1 - weight) * code_score + weight * llm.score
    return round(min(blended, code_score) if flagged or llm.flags else blended, 4)


def _cost(candidate: Candidate) -> Decimal:
    return candidate.total_cost if candidate.total_cost is not None else Decimal("Infinity")


def blend_ranking(
    ranking: RankingResult,
    assessments: Sequence[CredibilityAssessment],
    candidates: Mapping[str, Candidate],
    scores: Mapping[str, CandidateScore],
    *,
    weight: float,
) -> tuple[tuple[RankedViable, ...], RankingResult]:
    """Re-rank the already-filtered, unblocked candidates. The set of candidates never changes."""
    assessed = {a.candidate_id: a for a in assessments}
    viable: list[RankedViable] = []
    for item in ranking.ranked:
        llm = scores.get(item.candidate_id)
        flags = assessed[item.candidate_id].flags
        viable.append(
            RankedViable(
                candidate_id=item.candidate_id,
                score=blend_score(item.score, llm, flagged=bool(flags), weight=weight),
                code_score=item.score,
                llm_score=llm.score if llm else None,
                credibility_flags=flags,
                llm_flags=llm.flags if llm else (),
                llm_reason=llm.reason if llm else None,
            )
        )
    viable.sort(
        key=lambda v: (-v.score, -assessed[v.candidate_id].trust_score, _cost(candidates[v.candidate_id]), v.candidate_id)
    )
    by_id = {r.candidate_id: r for r in ranking.ranked}
    ranked = tuple(
        RankedCandidate(
            candidate_id=v.candidate_id,
            score=v.score,
            breakdown={**by_id[v.candidate_id].breakdown, **({"llm": v.llm_score} if v.llm_score is not None else {})},
        )
        for v in viable
    )
    margin = round(ranked[0].score - ranked[1].score, 4) if len(ranked) >= 2 else None
    return tuple(viable), RankingResult(ranked=ranked, weights=ranking.weights, margin=margin)
