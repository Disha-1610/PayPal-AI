"""Phase 3 pipeline: hard filter -> credibility -> ranking -> outcome. Deterministic end to end.

Credibility is assessed on the whole pool (price outliers need the baseline); only hard-filter
survivors are ranked. The returned report is the evaluation trace: every intermediate result.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.audit.events import AuditScope, Phase
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


class PipelineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    filtered: FilterTrace  # ids only: the report never carries candidate free text
    assessments: tuple[CredibilityAssessment, ...]
    ranking: RankingResult
    outcome: EvaluationOutcome


def ensure_unique_ids(candidates: Sequence[Candidate]) -> None:
    ids = [c.candidate_id for c in candidates]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate candidate ids in pool: {duplicates}")


def evaluate(
    candidates: Sequence[Candidate],
    constraints: Sequence[Constraint],
    *,
    now: datetime,
    weights: Mapping[str, float] | None = None,
    attempt: int = 0,
    credibility_policy: CredibilityPolicy | None = None,
    outcome_policy: OutcomePolicy | None = None,
    audit: AuditScope | None = None,
) -> PipelineResult:
    ensure_unique_ids(candidates)
    filtered = hard_filter(candidates, constraints)
    assessments = assess_credibility(candidates, now=now, policy=credibility_policy)
    ranking = rank_candidates(filtered.survivors, assessments, constraints, weights)
    outcome = classify_outcome(filtered, assessments, ranking, attempt=attempt, policy=outcome_policy)
    report = PipelineResult(filtered=filtered.trace(), assessments=assessments, ranking=ranking, outcome=outcome)
    if audit:
        _audit(audit, report, attempt)
    return report


def _audit(audit: AuditScope, report: PipelineResult, attempt: int) -> None:
    """Ids, codes and scores only: the trace never carries candidate free text."""
    for r in report.filtered.rejections:
        audit.emit(
            Phase.EVALUATION, "CANDIDATE_REJECTED", node="evaluation", resource_type="candidate", resource_id=r.candidate_id,
            decision=r.kind.value, reason=r.reason, metadata={"constraint_id": r.constraint_id, "field": r.field_name},
        )
    audit.emit(
        Phase.EVALUATION, "CANDIDATES_RANKED", node="evaluation", decision="ok",
        metadata={"ranked": [{"id": c.candidate_id, "score": c.score} for c in report.ranking.ranked], "margin": report.ranking.margin},
    )
    audit.emit(
        Phase.EVALUATION, "EVALUATION_COMPLETED", node="evaluation", decision=report.outcome.kind.value,
        reason=report.outcome.reason, resource_id=report.outcome.selected_candidate_id,
        metadata={"attempt": attempt, "survivors": len(report.filtered.survivor_ids)},
    )
