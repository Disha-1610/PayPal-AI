from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.orchestrator.agents.evaluation.credibility import assess_credibility
from app.orchestrator.agents.evaluation.hard_filter import hard_filter
from app.orchestrator.agents.evaluation.outcome import OutcomePolicy, classify_outcome
from app.orchestrator.agents.evaluation.ranking import rank_candidates
from app.schemas.candidate import Candidate, DataQuality
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from app.schemas.evaluation import EvaluationOutcome, OutcomeKind

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _con(field, op, value, strength=ConstraintStrength.HARD):
    return Constraint(
        field_name=field, operator=op, target_value=value, source=ConstraintSource.USER, strength=strength
    )


def _cand(cid, **kw):
    kw.setdefault("total_cost", Decimal("1000"))
    kw.setdefault("review_count", 100)
    kw.setdefault("rating", 4.5)
    return Candidate(candidate_id=cid, source="t", domain="contractor", title=cid, fetched_at=NOW, **kw)


def _run(cands, constraints, *, weights=None, attempt=0, policy=None):
    filtered = hard_filter(cands, constraints)
    assessments = assess_credibility(cands, now=NOW)
    survivors = list(filtered.survivors)
    ranking = rank_candidates(survivors, assessments, constraints, weights)
    return classify_outcome(filtered, assessments, ranking, attempt=attempt, policy=policy)


BUDGET = _con("budget", Operator.LTE, 8000)


def test_clean_winner_is_a_plain_recommendation():
    out = _run([_cand("A", total_cost=Decimal("7000")), _cand("B", review_count=0, total_cost=Decimal("7000"))], [BUDGET])
    assert out.kind is OutcomeKind.RECOMMEND and out.selected_candidate_id == "A" and out.caveats == ()


def test_winner_flags_and_unverifiable_exclusions_become_caveats():
    licensed = _con("licensed", Operator.EQ, True)
    winner = _cand("A", review_count=5, attributes={"licensed": True})
    unknown = _cand("F", quality={"licensed": DataQuality.MISSING})
    out = _run([winner, unknown], [licensed])
    assert out.kind is OutcomeKind.RECOMMEND_WITH_CAVEATS and out.selected_candidate_id == "A"
    assert "credibility: few_reviews" in out.caveats
    assert any("could not be verified" in c for c in out.caveats)


def test_near_tie_asks_the_user_to_choose():
    out = _run([_cand("A"), _cand("B")], [BUDGET])
    assert out.kind is OutcomeKind.NEEDS_USER_INPUT and set(out.options) == {"A", "B"}


def test_only_unverifiable_candidates_ask_for_verification():
    licensed = _con("licensed", Operator.EQ, True)
    out = _run([_cand("F", quality={"licensed": DataQuality.MISSING})], [licensed])
    assert out.kind is OutcomeKind.NEEDS_USER_INPUT and out.options == ("F",)


def test_all_filtered_out_retries_relaxing_the_most_restrictive_constraint():
    start = _con("start_within_days", Operator.LTE, 30)
    cands = [
        _cand(c, total_cost=Decimal("7000"), attributes={"start_within_days": 60}) for c in ("A", "B", "C")
    ] + [_cand("D", total_cost=Decimal("9000"), attributes={"start_within_days": 10})]
    out = _run(cands, [BUDGET, start])
    assert out.kind is OutcomeKind.RETRY_SEARCH and out.relax_constraint_id == start.constraint_id


def test_empty_discovery_retries_without_a_constraint_to_relax():
    out = _run([], [BUDGET])
    assert out.kind is OutcomeKind.RETRY_SEARCH and out.relax_constraint_id is None


def test_retries_are_capped():
    assert _run([], [BUDGET], attempt=2).kind is OutcomeKind.NO_RESULT
    assert _run([], [BUDGET], attempt=1, policy=OutcomePolicy(max_retries=5)).kind is OutcomeKind.RETRY_SEARCH


def test_all_candidates_failing_credibility_retries():
    bait = [
        _cand("X", review_count=0, total_cost=Decimal("100"), quality={"rating": DataQuality.UNRELIABLE}),
        _cand("b"),
        _cand("c"),
    ]
    out = _run(bait, [_con("budget", Operator.LTE, 150)])  # only the bait item passes the filter
    assert out.kind is OutcomeKind.RETRY_SEARCH and "failed credibility" in out.reason


def test_weak_best_score_is_not_recommended():
    weak = _cand("A", review_count=0, rating=None, quality={"rating": DataQuality.UNRELIABLE})
    pricey = _cand("B", total_cost=Decimal("9000"))  # eliminated by the budget: a re-search can relax it
    out = _run([weak, pricey], [BUDGET], policy=OutcomePolicy(min_score=0.9))
    assert out.kind is OutcomeKind.RETRY_SEARCH and "below the usable threshold" in out.reason


def test_weak_but_valid_candidates_go_to_the_user_when_a_new_search_cannot_help():
    weak = _cand("A", review_count=0, rating=None, quality={"rating": DataQuality.UNRELIABLE})
    nothing_to_relax = _run([weak], [], policy=OutcomePolicy(min_score=0.9))
    assert nothing_to_relax.kind is OutcomeKind.NEEDS_USER_INPUT and nothing_to_relax.options == ("A",)
    pricey = _cand("B", total_cost=Decimal("9000"))
    exhausted = _run([weak, pricey], [BUDGET], attempt=2, policy=OutcomePolicy(min_score=0.9))
    assert exhausted.kind is OutcomeKind.NEEDS_USER_INPUT and exhausted.options == ("A",)


def test_outcome_model_enforces_selection_invariant():
    with pytest.raises(ValueError, match="selected_candidate_id"):
        EvaluationOutcome(kind=OutcomeKind.RECOMMEND, reason="x")
    with pytest.raises(ValueError, match="selected_candidate_id"):
        EvaluationOutcome(kind=OutcomeKind.RETRY_SEARCH, reason="x", selected_candidate_id="A")
