"""Evaluator agent: the deterministic pipeline decides; an optional LLM layer only adds reasoning.

  1. ``hard_filter``: hard constraints, code only. A rejection can never be undone by the model.
  2. ``assess_credibility``: flags (review spikes, price outliers) come from code.
  3. ``score_with_llm``: review / listing text -> strict ``LLMScores``. No tools, no routing power.
  4. ``blend_ranking``: code score + capped LLM score; the model can only lower a score, never promote.
  5. ``classify_outcome`` on the final ranking -> ``EvaluatorOutcome`` (VIABLE / NONE_VIABLE / RETRY).

The agent returns an ``EvaluationReport`` and a partial state update; ``router.evaluator`` picks the
next step. It never calls another agent. If the model output is unavailable or invalid after retries,
ranking falls back to code only, so the outcome is always defined.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime

from app.audit.events import AuditScope, Phase
from app.llm.providers import LLMProvider
from app.orchestrator.agents.evaluation.blend import blend_ranking
from app.orchestrator.agents.evaluation.credibility import (
    CredibilityPolicy,
    assess_credibility,
)
from app.orchestrator.agents.evaluation.diagnosis import diagnose
from app.orchestrator.agents.evaluation.hard_filter import hard_filter
from app.orchestrator.agents.evaluation.outcome import OutcomePolicy, classify_outcome
from app.orchestrator.agents.evaluation.pipeline import ensure_unique_ids
from app.orchestrator.agents.evaluation.ranking import rank_candidates
from app.orchestrator.agents.evaluator.llm_scoring import NO_TEXT, score_with_llm
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint
from app.schemas.evaluation import (
    CredibilityAssessment,
    EvaluationOutcome,
    HardFilterResult,
    OutcomeKind,
    RejectionKind,
)
from app.schemas.evaluator import (
    CandidateScore,
    EvaluationReport,
    EvaluatorOutcome,
    EvaluatorPolicy,
    LLMStatus,
    RejectedCandidate,
    TextSource,
)
from app.schemas.state import TaskState

EVALUATOR_WRITABLE = frozenset({"evaluation", "status"})

__all__ = ["EVALUATOR_WRITABLE", "build_rejections", "evaluate_candidates", "evaluator_node", "map_outcome"]


def map_outcome(outcome: EvaluationOutcome, *, has_ranked: bool) -> tuple[EvaluatorOutcome, bool]:
    """``(outcome, needs_choice)``. A near-tie still has viable candidates; the user picks."""
    match outcome.kind:
        case OutcomeKind.RECOMMEND | OutcomeKind.RECOMMEND_WITH_CAVEATS:
            return EvaluatorOutcome.VIABLE, False
        case OutcomeKind.NEEDS_USER_INPUT if has_ranked:
            return EvaluatorOutcome.VIABLE, True
        case OutcomeKind.RETRY_SEARCH:
            return EvaluatorOutcome.RETRY, False
        case _:
            return EvaluatorOutcome.NONE_VIABLE, False


def build_rejections(
    candidates: Sequence[Candidate],
    constraints: Sequence[Constraint],
    filtered: HardFilterResult,
    assessments: Sequence[CredibilityAssessment],
) -> tuple[RejectedCandidate, ...]:
    """One reason per non-viable candidate, from field names, operators and targets only (no free text)."""
    unverifiable_fields: dict[str, list[str]] = defaultdict(list)
    for r in filtered.rejections:
        if r.kind is RejectionKind.UNVERIFIABLE:
            unverifiable_fields[r.candidate_id].append(r.field_name)

    rejected: list[RejectedCandidate] = []
    for d in diagnose(candidates, constraints):
        parts = [f"{v.constraint.field_name} must be {v.constraint.operator.value} {v.constraint.target_value}" for v in d.violations]
        fields = [v.constraint.field_name for v in d.violations]
        if d.unverifiable:
            parts.append(f"cannot verify: {', '.join(unverifiable_fields[d.candidate_id])}")
            fields.extend(unverifiable_fields[d.candidate_id])
        kind = RejectionKind.FAILED if d.violations else RejectionKind.UNVERIFIABLE
        rejected.append(RejectedCandidate(candidate_id=d.candidate_id, kind=kind, fields=tuple(fields), reason="; ".join(parts)))
    for a in assessments:
        if a.blocked and a.candidate_id in {c.candidate_id for c in filtered.survivors}:
            flags = ", ".join(f.value for f in a.flags)
            rejected.append(RejectedCandidate(candidate_id=a.candidate_id, reason=f"blocked by credibility: {flags}"))
    return tuple(rejected)


async def evaluate_candidates(
    candidates: Sequence[Candidate],
    constraints: Sequence[Constraint],
    *,
    now: datetime,
    text_source: TextSource,
    weights: Mapping[str, float] | None = None,
    attempt: int = 0,
    policy: EvaluatorPolicy | None = None,
    credibility_policy: CredibilityPolicy | None = None,
    outcome_policy: OutcomePolicy | None = None,
    provider: LLMProvider | None = None,
    audit: AuditScope | None = None,
) -> EvaluationReport:
    policy = policy or EvaluatorPolicy()
    ensure_unique_ids(candidates)

    filtered = hard_filter(candidates, constraints)
    assessments = assess_credibility(candidates, now=now, policy=credibility_policy)
    code_ranking = rank_candidates(filtered.survivors, assessments, constraints, weights)

    by_id = {c.candidate_id: c for c in filtered.survivors}
    scores: Mapping[str, CandidateScore] = {}
    status, why = LLMStatus.SKIPPED, "llm disabled" if not policy.use_llm else ""
    if policy.use_llm and code_ranking.ranked:
        top = [by_id[r.candidate_id] for r in code_ranking.ranked[: policy.max_llm_candidates]]
        result, why = await score_with_llm(top, text_source, policy, provider)
        if result is not None:
            scores, status = result, LLMStatus.USED
        else:
            status = LLMStatus.SKIPPED if why == NO_TEXT else LLMStatus.FALLBACK
    if audit and status is not LLMStatus.USED and policy.use_llm and code_ranking.ranked:
        audit.emit(
            Phase.EVALUATION, "LLM_SCORING_FALLBACK" if status is LLMStatus.FALLBACK else "LLM_SCORING_SKIPPED",
            node="evaluator", actor_type="agent", decision=status.value, reason=why,
        )

    viable, ranking = blend_ranking(code_ranking, assessments, by_id, scores, weight=policy.llm_weight)
    outcome = classify_outcome(filtered, assessments, ranking, attempt=attempt, policy=outcome_policy)
    kind, needs_choice = map_outcome(outcome, has_ranked=bool(ranking.ranked))
    report = EvaluationReport(
        outcome=kind, reason=outcome.reason, attempt=attempt, ranked=viable,
        rejected=build_rejections(candidates, constraints, filtered, assessments), margin=ranking.margin,
        needs_choice=needs_choice, relax_constraint_id=outcome.relax_constraint_id, llm_status=status, evaluated_at=now,
    )
    if audit:
        _audit(audit, report, scores)
    return report


def _audit(audit: AuditScope, report: EvaluationReport, scores: Mapping[str, CandidateScore]) -> None:
    """Ids, codes and scores only; the model's reason text is stored as a reason, never routed on."""
    for r in report.rejected:
        audit.emit(
            Phase.EVALUATION, "CANDIDATE_REJECTED", node="evaluator", resource_type="candidate", resource_id=r.candidate_id,
            decision=r.kind.value if r.kind else "blocked", reason=r.reason, metadata={"fields": list(r.fields)},
        )
    for v in report.ranked:
        if v.candidate_id in scores:
            audit.emit(
                Phase.EVALUATION, "CANDIDATE_LLM_SCORED", node="evaluator", actor_type="agent", resource_type="candidate",
                resource_id=v.candidate_id, decision=f"{v.llm_score:.2f}", reason=v.llm_reason,
                metadata={"flags": [f.value for f in v.llm_flags], "code_score": v.code_score, "final_score": v.score},
            )
    audit.emit(
        Phase.EVALUATION, "CANDIDATES_RANKED", node="evaluator", decision="ok",
        metadata={"ranked": [{"id": v.candidate_id, "score": v.score} for v in report.ranked], "margin": report.margin},
    )
    audit.emit(
        Phase.EVALUATION, "EVALUATION_COMPLETED", node="evaluator", decision=report.outcome.value, reason=report.reason,
        resource_id=report.ranked[0].candidate_id if report.ranked else None,
        metadata={
            "attempt": report.attempt, "viable": len(report.ranked), "rejected": len(report.rejected),
            "llm_status": report.llm_status.value, "needs_choice": report.needs_choice,
        },
    )


async def evaluator_node(
    state: TaskState,
    *,
    now: datetime,
    text_source: TextSource,
    weights: Mapping[str, float] | None = None,
    policy: EvaluatorPolicy | None = None,
    credibility_policy: CredibilityPolicy | None = None,
    outcome_policy: OutcomePolicy | None = None,
    provider: LLMProvider | None = None,
    audit: AuditScope | None = None,
) -> tuple[EvaluationReport, dict[str, object]]:
    """Evaluate the pool in state. The attempt counter advances on every re-evaluation (RETRY cap)."""
    previous = state.get("evaluation")
    report = await evaluate_candidates(
        state.get("candidates", []), state.get("constraints", []), now=now, text_source=text_source, weights=weights,
        attempt=previous.attempt + 1 if previous else 0, policy=policy, credibility_policy=credibility_policy,
        outcome_policy=outcome_policy, provider=provider, audit=audit,
    )
    return report, {"evaluation": report, "status": "evaluated"}
