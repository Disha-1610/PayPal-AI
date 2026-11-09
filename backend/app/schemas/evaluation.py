"""Evaluation (Phase 3) result models. Produced by deterministic code only, never by the LLM."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.candidate import Candidate


class RejectionKind(StrEnum):
    FAILED = "failed"  # the candidate's value violates the constraint
    UNVERIFIABLE = "unverifiable"  # the value is missing/unreliable or of the wrong type, so it cannot be checked


class Rejection(BaseModel):
    """Why one candidate did not pass one hard constraint."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    constraint_id: str
    field_name: str
    kind: RejectionKind
    reason: str


class _RejectionView(BaseModel):
    """Shared read helpers over a list of rejections."""

    model_config = ConfigDict(frozen=True)

    rejections: tuple[Rejection, ...]

    @property
    def failed_ids(self) -> frozenset[str]:
        return frozenset(r.candidate_id for r in self.rejections if r.kind is RejectionKind.FAILED)

    @property
    def unverifiable_ids(self) -> frozenset[str]:
        """Candidates excluded only because a hard field could not be verified (none failed)."""
        flagged = frozenset(r.candidate_id for r in self.rejections if r.kind is RejectionKind.UNVERIFIABLE)
        return flagged - self.failed_ids

    def eliminations_by_constraint(self) -> dict[str, int]:
        """Candidates removed per constraint id; lets Phase 4 pick the constraint worth relaxing."""
        counts: dict[str, set[str]] = {}
        for r in self.rejections:
            counts.setdefault(r.constraint_id, set()).add(r.candidate_id)
        return {cid: len(ids) for cid, ids in counts.items()}


class FilterTrace(_RejectionView):
    """Id-only record of step 3.1 for the evaluation trace: no candidate free text."""

    survivor_ids: tuple[str, ...]


class HardFilterResult(_RejectionView):
    """Funnel output of step 3.1. Every input candidate is in ``survivors`` or has a rejection."""

    survivors: tuple[Candidate, ...]

    def trace(self) -> FilterTrace:
        return FilterTrace(survivor_ids=tuple(c.candidate_id for c in self.survivors), rejections=self.rejections)


class CredibilityFlag(StrEnum):
    NO_REVIEWS = "no_reviews"
    FEW_REVIEWS = "few_reviews"
    STALE_REVIEWS = "stale_reviews"
    REVIEW_BURST = "review_burst"  # many reviews squeezed into a short window
    HIGH_COMPLAINTS = "high_complaints"
    UNRELIABLE_DATA = "unreliable_data"
    STALE_DATA = "stale_data"
    MISSING_RATING = "missing_rating"
    PRICE_OUTLIER_LOW = "price_outlier_low"  # far below the pool median: scam / bait pricing signal


class CredibilityAssessment(BaseModel):
    """Step 3.2 output for one candidate. ``trust_score`` is a 0-1 ranking input, not a verdict."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    trust_score: float
    flags: tuple[CredibilityFlag, ...] = ()
    blocked: bool = False  # trust fell below the policy floor: exclude from ranking


TRUST_CRITERION = "trust"


class RankedCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    score: float
    breakdown: dict[str, float]  # criterion -> 0-1 score, before weighting


class RankingResult(BaseModel):
    """Step 3.3 output: best first. ``margin`` is the gap between the top two scores (None if < 2)."""

    model_config = ConfigDict(frozen=True)

    ranked: tuple[RankedCandidate, ...]
    weights: dict[str, float]  # normalised weights actually applied
    margin: float | None = None


class OutcomeKind(StrEnum):
    RECOMMEND = "recommend"
    RECOMMEND_WITH_CAVEATS = "recommend_with_caveats"
    NEEDS_USER_INPUT = "needs_user_input"  # near-tie, or only unverifiable candidates remain
    RETRY_SEARCH = "retry_search"  # hand back to discovery (Phase 4), optionally relaxing a constraint
    NO_RESULT = "no_result"  # retries exhausted


class EvaluationOutcome(BaseModel):
    """Step 3.4 output: what the agent does next. Built from ids and codes only, never candidate text."""

    model_config = ConfigDict(frozen=True)

    kind: OutcomeKind
    reason: str
    selected_candidate_id: str | None = None
    options: tuple[str, ...] = ()  # candidate ids to choose between / verify
    caveats: tuple[str, ...] = ()
    relax_constraint_id: str | None = None  # RETRY_SEARCH: the hard constraint that removed the most candidates

    @model_validator(mode="after")
    def _check_selection(self) -> Self:
        recommends = self.kind in (OutcomeKind.RECOMMEND, OutcomeKind.RECOMMEND_WITH_CAVEATS)
        if recommends != (self.selected_candidate_id is not None):
            raise ValueError("selected_candidate_id is required for, and only for, recommend outcomes")
        return self
