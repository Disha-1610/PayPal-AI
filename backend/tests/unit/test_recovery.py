from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.orchestrator.agents.adapt.recovery import is_source_locked, plan_adaptation
from app.schemas.adapt import AdaptPolicy, AdaptProgress, RecoveryStep, TerminationReason
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
STEP = RecoveryStep


def _c(field, op, value, strength=ConstraintStrength.HARD):
    return Constraint(field_name=field, operator=op, target_value=value, source=ConstraintSource.USER, strength=strength)


def _cand(cid, cost, age_seconds=0, **attrs):
    return Candidate(
        candidate_id=cid, source="t", domain="any", title=cid, total_cost=Decimal(cost),
        attributes=attrs, fetched_at=NOW - timedelta(seconds=age_seconds),
    )


BUDGET = _c("budget", Operator.LTE, 100)
LOCK = _c("vendor", Operator.EQ, "acme")
POOL = [_cand("A", 120, vendor="acme"), _cand("B", 110, vendor="acme")]


def _plan(progress, pool=POOL, constraints=(BUDGET,), **kw):
    return plan_adaptation(pool, list(constraints), progress, now=NOW, **kw)


def _walk(constraints, source_fields, pool=POOL, policy=None):
    """Run the ladder with a search that never finds anything new, until it stops or asks."""
    progress, steps = AdaptProgress(), []
    for _ in range(12):
        decision = _plan(progress, pool, constraints, source_fields=source_fields, policy=policy)
        progress = decision.progress
        steps.append(progress.next_step)
        if progress.next_step in (STEP.ASK_USER, STEP.STOP):
            return steps, decision
        # Simulate the rung returning the same pool: the next call sees a repeated hash.
    raise AssertionError(steps)


def test_flexible_task_climbs_local_then_cross_domain_then_asks():
    steps, decision = _walk([BUDGET], source_fields=())
    assert steps == [STEP.LOCAL_RECOVERY, STEP.CROSS_DOMAIN, STEP.ASK_USER]
    assert decision.proposals and decision.progress.prompts == 1


def test_source_lock_skips_cross_domain_expansion():
    assert is_source_locked([LOCK], ["vendor"]) and not is_source_locked([LOCK], ["region"])
    steps, _ = _walk([BUDGET, LOCK], source_fields=("vendor",))
    assert steps == [STEP.LOCAL_RECOVERY, STEP.ASK_USER]


def test_soft_or_inactive_lock_does_not_count():
    soft = _c("vendor", Operator.EQ, "acme", ConstraintStrength.SOFT)
    assert not is_source_locked([soft], ["vendor"])


def test_new_results_keep_the_current_rung_going_until_its_attempt_cap():
    policy = AdaptPolicy(max_local_attempts=2)
    progress = AdaptProgress()
    first = _plan(progress, [POOL[0]], policy=policy)
    second = _plan(first.progress, POOL, policy=policy)  # pool changed -> progress was made
    assert [first.progress.next_step, second.progress.next_step] == [STEP.LOCAL_RECOVERY, STEP.LOCAL_RECOVERY]
    third = _plan(second.progress, [*POOL, _cand("C", 130)], policy=policy)
    assert third.progress.next_step is STEP.CROSS_DOMAIN  # local cap reached


def test_attempt_cap_stops_with_a_report():
    decision = _plan(AdaptProgress(attempts=6), policy=AdaptPolicy(max_attempts=6))
    assert decision.progress.termination is TerminationReason.ATTEMPT_CAP
    assert "budget: 2 candidate(s)" in decision.progress.detail


def test_nothing_to_propose_means_exhausted():
    no_cost_data = [Candidate(candidate_id="U", source="t", domain="any", title="U")]  # unverifiable: nothing to relax
    steps, decision = _walk([BUDGET], (), pool=no_cost_data)
    assert steps[-1] is STEP.STOP and decision.progress.termination is TerminationReason.EXHAUSTED


def test_repeat_after_user_was_asked_is_no_progress():
    _, asked = _walk([BUDGET], ())
    again = _plan(asked.progress)  # the user's reply changed nothing: same ids, same constraints
    assert again.progress.termination is TerminationReason.NO_PROGRESS


def test_prompt_cap_stops_asking():
    _, asked = _walk([BUDGET], ())
    changed = _plan(asked.progress, [*POOL, _cand("C", 150)], policy=AdaptPolicy(max_user_prompts=1))
    assert changed.progress.termination is TerminationReason.PROMPT_CAP


def test_stale_pool_is_refreshed_once_before_asking():
    stale = [_cand("A", 120, age_seconds=3600), _cand("B", 110, age_seconds=3600)]
    steps, decision = _walk([BUDGET], (), pool=stale)
    assert steps == [STEP.LOCAL_RECOVERY, STEP.CROSS_DOMAIN, STEP.REFRESH_POOL, STEP.ASK_USER]
    assert decision.progress.refreshes == 1
