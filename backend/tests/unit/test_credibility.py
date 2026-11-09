from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.orchestrator.agents.evaluation.credibility import CredibilityPolicy, assess_credibility
from app.schemas.candidate import Candidate, DataQuality
from app.schemas.evaluation import CredibilityFlag as F

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def _cand(cid, **kw):
    kw.setdefault("rating", 4.5)
    kw.setdefault("review_count", 100)
    kw.setdefault("total_cost", Decimal("1000"))
    return Candidate(candidate_id=cid, source="t", domain="contractor", title=cid, fetched_at=NOW, **kw)


def _one(cand, pool=None):
    return assess_credibility(pool or [cand], now=NOW)[0]


def test_clean_candidate_has_full_trust():
    a = _one(_cand("A", latest_review_at=NOW - timedelta(days=5), complaint_count=1))
    assert a.trust_score == 1.0 and a.flags == () and not a.blocked


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"review_count": 0}, F.NO_REVIEWS),
        ({"review_count": None}, F.NO_REVIEWS),
        ({"review_count": 3}, F.FEW_REVIEWS),
        ({"latest_review_at": NOW - timedelta(days=400)}, F.STALE_REVIEWS),
        ({"complaint_count": 20}, F.HIGH_COMPLAINTS),
        (
            {"first_review_at": NOW - timedelta(days=3), "latest_review_at": NOW - timedelta(days=1)},
            F.REVIEW_BURST,
        ),
        ({"rating": None}, F.MISSING_RATING),
        ({"quality": {"rating": DataQuality.UNRELIABLE}}, F.UNRELIABLE_DATA),
        ({"quality": {"rating": DataQuality.STALE}}, F.STALE_DATA),
    ],
)
def test_each_signal_sets_flag_and_lowers_score(kwargs, flag):
    a = _one(_cand("A", **kwargs))
    assert flag in a.flags and a.trust_score < 1.0


def test_price_outlier_uses_pool_median_and_needs_three_priced_candidates():
    pool = [_cand("cheap", total_cost=Decimal("300")), _cand("b"), _cand("c")]
    by_id = {a.candidate_id: a for a in assess_credibility(pool, now=NOW)}
    assert F.PRICE_OUTLIER_LOW in by_id["cheap"].flags and F.PRICE_OUTLIER_LOW not in by_id["b"].flags
    small = assess_credibility(pool[:2], now=NOW)
    assert all(F.PRICE_OUTLIER_LOW not in a.flags for a in small)


def test_penalties_compound_and_floor_blocks():
    bad = _cand("X", review_count=0, rating=None, quality={"rating": DataQuality.UNRELIABLE})
    a = _one(bad)
    assert a.trust_score == round(0.5 * 0.8 * 0.6, 4) and not a.blocked
    pool = [_cand("X", review_count=0, total_cost=Decimal("100"), quality={"rating": DataQuality.UNRELIABLE}),
            _cand("b"), _cand("c")]
    x = assess_credibility(pool, now=NOW)[0]
    assert x.blocked and x.trust_score < 0.15


def test_custom_policy_and_empty_pool():
    policy = CredibilityPolicy(few_reviews_below=500)
    a = assess_credibility([_cand("A")], now=NOW, policy=policy)[0]
    assert F.FEW_REVIEWS in a.flags
    assert assess_credibility([], now=NOW) == ()


def test_burst_needs_dates_enough_reviews_and_a_short_span():
    spread = _cand("A", first_review_at=NOW - timedelta(days=300), latest_review_at=NOW - timedelta(days=2))
    few = _cand("B", review_count=2, first_review_at=NOW - timedelta(days=3), latest_review_at=NOW - timedelta(days=1))
    undated = _cand("C")
    assert all(F.REVIEW_BURST not in _one(c).flags for c in (spread, few, undated))


def test_contractor_example_burst_candidate_scores_about_point_three():
    a = _one(
        _cand("A", review_count=3, first_review_at=NOW - timedelta(days=6), latest_review_at=NOW - timedelta(days=1))
    )
    assert set(a.flags) == {F.FEW_REVIEWS, F.REVIEW_BURST} and a.trust_score == 0.28


def test_candidate_rejects_first_review_after_latest():
    with pytest.raises(ValueError, match="first_review_at"):
        _cand("X", first_review_at=NOW, latest_review_at=NOW - timedelta(days=1))
