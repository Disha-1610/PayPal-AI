from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.orchestrator.agents.evaluation.credibility import assess_credibility
from app.orchestrator.agents.evaluation.hard_filter import hard_filter
from app.orchestrator.agents.evaluation.ranking import normalise_weights, rank_candidates
from app.schemas.candidate import Candidate, DataQuality
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _con(field, op, value, strength=ConstraintStrength.SOFT):
    return Constraint(
        field_name=field, operator=op, target_value=value, source=ConstraintSource.USER, strength=strength
    )


def _cand(cid, **kw):
    kw.setdefault("total_cost", Decimal("1000"))
    return Candidate(candidate_id=cid, source="t", domain="contractor", title=cid, fetched_at=NOW, **kw)


def _rank(cands, constraints, weights=None):
    return rank_candidates(cands, assess_credibility(cands, now=NOW), constraints, weights)


def test_contractor_example_trusted_candidate_beats_burst_candidate():
    burst = _cand(
        "A",
        total_cost=Decimal("7200"),
        rating=4.8,
        review_count=3,
        attributes={"warranty_months": 12},
        first_review_at=NOW - timedelta(days=6),
        latest_review_at=NOW - timedelta(days=1),
    )
    clean = _cand(
        "B",
        total_cost=Decimal("7900"),
        rating=4.6,
        review_count=212,
        attributes={"warranty_months": 24},
        first_review_at=NOW - timedelta(days=700),
        latest_review_at=NOW - timedelta(days=2),
    )
    soft = [_con("rating", Operator.GTE, 4.5), _con("warranty_months", Operator.GTE, 24)]
    budget = _con("budget", Operator.LTE, 8000, ConstraintStrength.HARD)
    survivors = hard_filter([burst, clean], [budget]).survivors
    result = _rank(list(survivors), soft, {"trust": 0.4, "rating": 0.3, "warranty_months": 0.3})
    assert [r.candidate_id for r in result.ranked] == ["B", "A"]
    assert result.ranked[0].score == 1.0
    assert result.ranked[1].breakdown["warranty_months"] == 0.5  # 12 of 24 months
    assert result.margin is not None and result.margin > 0.3


def test_missing_data_scores_zero_and_unsatisfied_non_numeric_scores_zero():
    a = _cand("A", review_count=50, rating=4.9, attributes={"location": "pune"})
    result = _rank([a], [_con("warranty_months", Operator.GTE, 24), _con("location", Operator.EQ, "mumbai")])
    assert result.ranked[0].breakdown["warranty_months"] == 0.0
    assert result.ranked[0].breakdown["location"] == 0.0


def test_weights_shift_the_winner():
    cheap = _cand("cheap", review_count=50, rating=4.0, attributes={"warranty_months": 36})
    rated = _cand("rated", review_count=50, rating=4.9, attributes={"warranty_months": 6})
    soft = [_con("rating", Operator.GTE, 4.9), _con("warranty_months", Operator.GTE, 36)]
    assert _rank([cheap, rated], soft, {"rating": 5, "warranty_months": 0.1}).ranked[0].candidate_id == "rated"
    assert _rank([cheap, rated], soft, {"rating": 0.1, "warranty_months": 5}).ranked[0].candidate_id == "cheap"


def test_blocked_candidates_are_excluded_and_ties_break_deterministically():
    pool = [
        _cand("b", review_count=50, total_cost=Decimal("900")),
        _cand("a", review_count=50, total_cost=Decimal("900")),
        _cand("z", review_count=50, total_cost=Decimal("800")),
        _cand("bait", review_count=0, total_cost=Decimal("100"), quality={"rating": DataQuality.UNRELIABLE}),
    ]
    result = _rank(pool, [])
    assert [r.candidate_id for r in result.ranked] == ["z", "a", "b"]
    assert result.margin == 0.0


def test_single_candidate_has_no_margin_and_empty_pool_is_empty():
    assert _rank([_cand("a", review_count=50)], []).margin is None
    assert _rank([], []).ranked == ()


def test_weight_validation():
    assert normalise_weights(None, ["trust", "x"]) == {"trust": 0.5, "x": 0.5}
    with pytest.raises(ValueError, match="unknown"):
        normalise_weights({"nope": 1}, ["trust"])
    with pytest.raises(ValueError, match="non-negative"):
        normalise_weights({"trust": -1}, ["trust"])
    with pytest.raises(ValueError, match="positive"):
        normalise_weights({"trust": 0, "x": 0}, ["trust", "x"])


def test_missing_assessment_and_duplicate_soft_fields_are_rejected():
    cand = _cand("a", review_count=50)
    with pytest.raises(ValueError, match="no credibility assessment"):
        rank_candidates([cand], [], [])
    dup = [_con("rating", Operator.GTE, 4), _con("rating", Operator.LTE, 5)]
    with pytest.raises(ValueError, match="duplicate"):
        _rank([cand], dup)
