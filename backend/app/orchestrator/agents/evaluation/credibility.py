"""Step 3.2 - credibility. Pure, deterministic code: no LLM, no free text.

Uses only structured signals already on ``Candidate`` (counts, dates, quality flags, price
vs. the pool). Each signal multiplies the trust score by a penalty. Candidates are only
``blocked`` when the score falls below a hard floor; otherwise the score feeds ranking (3.3).
"""

import statistics
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.candidate import Candidate, DataQuality
from app.schemas.evaluation import CredibilityAssessment, CredibilityFlag

_MIN_POOL_FOR_PRICE_CHECK = 3


class CredibilityPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    few_reviews_below: int = Field(default=10, ge=1)
    stale_reviews_after: timedelta = timedelta(days=365)
    burst_min_reviews: int = Field(default=3, ge=2)
    burst_window: timedelta = timedelta(days=7)
    complaint_ratio_above: float = Field(default=0.1, gt=0)
    price_outlier_ratio: float = Field(default=0.4, gt=0, lt=1)
    block_below: float = Field(default=0.15, ge=0, le=1)
    penalties: dict[CredibilityFlag, float] = Field(
        default_factory=lambda: {
            CredibilityFlag.NO_REVIEWS: 0.5,
            CredibilityFlag.FEW_REVIEWS: 0.7,
            CredibilityFlag.STALE_REVIEWS: 0.8,
            CredibilityFlag.REVIEW_BURST: 0.4,
            CredibilityFlag.HIGH_COMPLAINTS: 0.6,
            CredibilityFlag.UNRELIABLE_DATA: 0.6,
            CredibilityFlag.STALE_DATA: 0.9,
            CredibilityFlag.MISSING_RATING: 0.8,
            CredibilityFlag.PRICE_OUTLIER_LOW: 0.4,
        }
    )


def _median_cost(candidates: Sequence[Candidate]) -> Decimal | None:
    costs = [c.total_cost for c in candidates if c.total_cost is not None]
    return statistics.median(costs) if len(costs) >= _MIN_POOL_FOR_PRICE_CHECK else None


def _flags(
    candidate: Candidate, *, now: datetime, median_cost: Decimal | None, policy: CredibilityPolicy
) -> list[CredibilityFlag]:
    flags: list[CredibilityFlag] = []
    reviews = candidate.review_count or 0
    if reviews == 0:
        flags.append(CredibilityFlag.NO_REVIEWS)
    elif reviews < policy.few_reviews_below:
        flags.append(CredibilityFlag.FEW_REVIEWS)
    if candidate.latest_review_at and now - candidate.latest_review_at > policy.stale_reviews_after:
        flags.append(CredibilityFlag.STALE_REVIEWS)
    if (
        candidate.first_review_at
        and candidate.latest_review_at
        and reviews >= policy.burst_min_reviews
        and candidate.latest_review_at - candidate.first_review_at <= policy.burst_window
    ):
        flags.append(CredibilityFlag.REVIEW_BURST)
    if candidate.complaint_count and candidate.complaint_count / max(reviews, 1) > policy.complaint_ratio_above:
        flags.append(CredibilityFlag.HIGH_COMPLAINTS)
    if candidate.rating is None:
        flags.append(CredibilityFlag.MISSING_RATING)
    quality = set(candidate.quality.values())
    if DataQuality.UNRELIABLE in quality:
        flags.append(CredibilityFlag.UNRELIABLE_DATA)
    if DataQuality.STALE in quality:
        flags.append(CredibilityFlag.STALE_DATA)
    if (
        median_cost
        and candidate.total_cost is not None
        and candidate.total_cost < median_cost * Decimal(str(policy.price_outlier_ratio))
    ):
        flags.append(CredibilityFlag.PRICE_OUTLIER_LOW)
    return flags


def assess_credibility(
    candidates: Sequence[Candidate], *, now: datetime, policy: CredibilityPolicy | None = None
) -> tuple[CredibilityAssessment, ...]:
    """Assess every candidate. Pass the whole discovered pool so the price check has a baseline."""
    policy = policy or CredibilityPolicy()
    median_cost = _median_cost(candidates)
    results: list[CredibilityAssessment] = []
    for candidate in candidates:
        flags = _flags(candidate, now=now, median_cost=median_cost, policy=policy)
        score = 1.0
        for flag in flags:
            score *= policy.penalties[flag]
        score = round(score, 4)
        results.append(
            CredibilityAssessment(
                candidate_id=candidate.candidate_id,
                trust_score=score,
                flags=tuple(flags),
                blocked=score < policy.block_below,
            )
        )
    return tuple(results)
