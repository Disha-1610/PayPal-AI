"""Phase 4.2 / 4.6 / 4.7 - recovery ladder, termination and no-progress detection. Pure decision logic.

Called after the evaluation outcome is ``RETRY_SEARCH`` (or discovery found nothing). It never searches
itself: it records the attempt and says which rung to run next, so it is testable without connectors.

Ladder:  local recovery (inside the user's source lock)  ->  cross-domain expansion (only if nothing
is locked)  ->  refresh a stale pool  ->  ask the user (``interrupt``)  ->  stop.

No-progress (4.7): a repeated ``progress_hash`` means the rung just run found nothing new, so that
rung is marked exhausted and the ladder moves on. Once the user has already been asked, a repeat
means even their answer changed nothing and the task stops with ``NO_PROGRESS``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.orchestrator.agents.evaluation.diagnosis import diagnose, hard_constraints, summarize
from app.orchestrator.agents.adapt.relaxation import propose_relaxations
from app.orchestrator.progress.hashing import progress_hash
from app.schemas.adapt import (
    AdaptPolicy,
    AdaptProgress,
    RecoveryStep,
    RelaxationProposal,
    TerminationReason,
)
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint


@dataclass(frozen=True, slots=True)
class AdaptDecision:
    progress: AdaptProgress  # ``next_step`` says what to run; ``termination`` is set when it is STOP
    proposals: tuple[RelaxationProposal, ...] = ()


def is_source_locked(constraints: Iterable[Constraint], source_fields: Iterable[str]) -> bool:
    """True when the user hard-locked where results may come from (vendor, platform, method)."""
    locked = frozenset(source_fields)
    return any(c.field_name in locked for c in hard_constraints(constraints))


def terminate(progress: AdaptProgress, reason: TerminationReason, detail: str = "") -> AdaptProgress:
    return progress.model_copy(update={"next_step": RecoveryStep.STOP, "termination": reason, "detail": detail})


def _is_stale(candidates: Sequence[Candidate], now: datetime, policy: AdaptPolicy) -> bool:
    limit = timedelta(seconds=policy.max_pool_age_seconds)
    return any(now - c.fetched_at > limit for c in candidates)


def _exhaust_last_rung(progress: AdaptProgress) -> AdaptProgress:
    """The rung that just ran changed nothing: do not run it again."""
    if progress.next_step is RecoveryStep.LOCAL_RECOVERY:
        return progress.model_copy(update={"local_exhausted": True})
    if progress.next_step is RecoveryStep.CROSS_DOMAIN:
        return progress.model_copy(update={"cross_domain_exhausted": True})
    return progress


def plan_adaptation(
    candidates: Sequence[Candidate],
    constraints: Sequence[Constraint],
    progress: AdaptProgress,
    *,
    now: datetime,
    policy: AdaptPolicy | None = None,
    source_fields: Iterable[str] = (),
    excluded_ids: frozenset[str] = frozenset(),
) -> AdaptDecision:
    policy = policy or AdaptPolicy()
    digest = progress_hash((c.candidate_id for c in candidates), constraints)
    repeated = digest in progress.seen_hashes
    current = progress.model_copy(
        update={"attempts": progress.attempts + 1, "seen_hashes": (*progress.seen_hashes, digest)}
    )

    def stop(reason: TerminationReason) -> AdaptDecision:
        return AdaptDecision(terminate(current, reason, summarize(diagnose(candidates, constraints))))

    if current.attempts > policy.max_attempts:
        return stop(TerminationReason.ATTEMPT_CAP)
    if repeated and current.prompts > 0:
        return stop(TerminationReason.NO_PROGRESS)
    if repeated:
        current = _exhaust_last_rung(current)

    if not current.local_exhausted:
        if current.local_attempts < policy.max_local_attempts:
            return _run(current, RecoveryStep.LOCAL_RECOVERY, local_attempts=current.local_attempts + 1)
        current = current.model_copy(update={"local_exhausted": True})

    if not is_source_locked(constraints, source_fields) and not current.cross_domain_exhausted:
        if current.cross_domain_attempts < policy.max_cross_domain_attempts:
            return _run(current, RecoveryStep.CROSS_DOMAIN, cross_domain_attempts=current.cross_domain_attempts + 1)
        current = current.model_copy(update={"cross_domain_exhausted": True})

    if current.refreshes < policy.max_refreshes and _is_stale(candidates, now, policy):
        return _run(current, RecoveryStep.REFRESH_POOL, refreshes=current.refreshes + 1)

    proposals = propose_relaxations(candidates, constraints, policy=policy, excluded_ids=excluded_ids)
    if not proposals:
        return stop(TerminationReason.EXHAUSTED)
    if current.prompts >= policy.max_user_prompts:
        return stop(TerminationReason.PROMPT_CAP)
    return AdaptDecision(
        _run(current, RecoveryStep.ASK_USER, prompts=current.prompts + 1).progress, tuple(proposals)
    )


def _run(progress: AdaptProgress, step: RecoveryStep, **counters: int) -> AdaptDecision:
    return AdaptDecision(progress.model_copy(update={"next_step": step, **counters}))
