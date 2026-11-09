"""Phase 3 pipeline: hard filter -> credibility -> ranking -> outcome. Deterministic end to end.

Credibility is assessed on the whole pool (price outliers need the baseline); only hard-filter
survivors are ranked. The returned report is the evaluation trace: every intermediate result.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.orchestrator.agents.evaluation.credibility import CredibilityPolicy, assess_credibility
from app.orchestrator.agents.evaluation.hard_filter import hard_filter
from app.orchestrator.agents.evaluation.outcome import OutcomePolicy, classify_outcome
from app.orchestrator.agents.evaluation.ranking import rank_candidates
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint
from app.schemas.evaluation import (
    CredibilityAssessment,
    EvaluationOutcome,
    FilterTrace,
    RankingResult,
)


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    filtered: FilterTrace  # ids only: the report never carries candidate free text
    assessments: tuple[CredibilityAssessment, ...]
    ranking: RankingResult
    outcome: EvaluationOutcome


def evaluate(
    candidates: Sequence[Candidate],
    constraints: Sequence[Constraint],
    *,
    now: datetime,
    weights: Mapping[str, float] | None = None,
    attempt: int = 0,
    credibility_policy: CredibilityPolicy | None = None,
    outcome_policy: OutcomePolicy | None = None,
) -> EvaluationReport:
    ids = [c.candidate_id for c in candidates]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate candidate ids in pool: {duplicates}")

    filtered = hard_filter(candidates, constraints)
    assessments = assess_credibility(candidates, now=now, policy=credibility_policy)
    ranking = rank_candidates(filtered.survivors, assessments, constraints, weights)
    outcome = classify_outcome(filtered, assessments, ranking, attempt=attempt, policy=outcome_policy)
    return EvaluationReport(filtered=filtered.trace(), assessments=assessments, ranking=ranking, outcome=outcome)
