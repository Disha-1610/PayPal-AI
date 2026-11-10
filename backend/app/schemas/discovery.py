"""Discovery (agent 3) models: policy, thin-data report, query-rewrite output and the run outcome.

Produced by deterministic code. The only model output is ``QueryRewrites``, and every string in it
is checked by ``orchestrator.guard.query_guard`` before it is used.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.candidate import Candidate


class QueryRung(StrEnum):
    """Fixed order of query adaptation: synonyms, then drop a soft term, then widen the category."""

    SYNONYMS = "synonyms"
    DROP_SOFT_TERM = "drop_soft_term"
    WIDEN_CATEGORY = "widen_category"


class ThinReason(StrEnum):
    TOO_FEW = "too_few"  # fewer than K candidates
    MISSING_FIELDS = "missing_fields"  # fewer than K candidates have every required field
    PLATFORM_FAILURE = "platform_failure"  # a search call still failed after its retries


class StopReason(StrEnum):
    SUFFICIENT = "sufficient"
    EXHAUSTED = "exhausted"  # every platform and every rung was tried
    ROUND_CAP = "round_cap"


class DiscoveryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_candidates: int = Field(default=3, ge=1, description="K: fewer complete candidates than this is thin")
    enrich_top_n: int = Field(default=5, ge=0, description="Candidates per round that get details/reviews/stock/delivery")
    max_rounds: int = Field(default=8, ge=1, description="Search rounds across all platforms, rewrites included")
    max_search_attempts: int = Field(default=3, ge=1, description="Retries of a failing connector inside one round")
    max_synonyms: int = Field(default=2, ge=0)
    max_soft_drops: int = Field(default=2, ge=0)
    search_limit: int = Field(default=10, ge=1, le=50)
    reviews_limit: int = Field(default=20, ge=1, le=100)
    required_fields: tuple[str, ...] = ("total_cost", "rating", "review_count", "available_by")

    @model_validator(mode="after")
    def _check_fields(self) -> Self:
        unknown = set(self.required_fields) - set(Candidate.model_fields)
        if unknown:
            raise ValueError(f"required_fields are not Candidate fields: {sorted(unknown)}")
        return self


class ThinReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    reasons: tuple[ThinReason, ...] = ()
    total: int = 0
    complete: int = 0

    @property
    def thin(self) -> bool:
        return bool(self.reasons)

    @property
    def query_fixable(self) -> bool:
        """A different query can add candidates; it cannot repair a platform whose call failed this round."""
        reasons = set(self.reasons)
        return ThinReason.PLATFORM_FAILURE not in reasons and bool({ThinReason.TOO_FEW, ThinReason.MISSING_FIELDS} & reasons)


class QueryRewrites(BaseModel):
    """The model's whole output: proposed replacement query strings, nothing else. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    queries: list[str] = Field(default_factory=list, max_length=5)


class DiscoveryRoute(StrEnum):
    """What the Router decides after each discovery step. Never produced by the model."""

    SEARCH = "search"  # a query is ready and not yet searched
    ADAPT_QUERY = "adapt_query"  # same platform, next rung of the query ladder
    NEXT_PLATFORM = "next_platform"
    EVALUATE = "evaluate"  # enough data, or nothing left to try: finish and pass the pool on


class QueryPlan(BaseModel):
    """The query text: the goal plus soft terms (what ladder rung 2 may drop)."""

    model_config = ConfigDict(frozen=True)

    core: str = Field(min_length=1)
    soft_terms: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return " ".join((self.core, *self.soft_terms))

    def drop_soft(self, count: int) -> "QueryPlan":
        """Drop the ``count`` last-stated soft terms."""
        return QueryPlan(core=self.core, soft_terms=self.soft_terms[: max(len(self.soft_terms) - count, 0)])


class LadderState(BaseModel):
    """Position in the query ladder for one platform. Resumable: no in-memory queue is lost."""

    model_config = ConfigDict(frozen=True)

    rung_index: int = 0
    loaded: bool = False  # whether the current rung's queue has been built
    queue: tuple[QueryPlan, ...] = ()
    tried: tuple[str, ...] = ()  # normalised query texts already searched or queued


class DiscoveryProgress(BaseModel):
    """One discovery run's loop state (``TaskState.discovery``). Written only by the discovery nodes."""

    model_config = ConfigDict(frozen=True)

    platform_index: int = 0
    rounds: int = 0
    plan: QueryPlan | None = Field(default=None, description="Query being searched or about to be; None = the base query")
    plan_searched: bool = False
    first_round: bool = True  # first round on this platform: no-progress is only judged on rewrites
    no_progress: bool = False
    ladder: LadderState = LadderState()
    ladder_exhausted: bool = False
    rewrite_cache: dict[str, tuple[str, ...]] = Field(default_factory=dict, description="Validated rewrites per rung+query")
    queries: tuple[str, ...] = ()
    seen_hashes: tuple[str, ...] = ()
    enriched: tuple[str, ...] = Field(default=(), description="'source:candidate_id' keys already enriched")
    unavailable: tuple[str, ...] = Field(default=(), description="Keys dropped because stock said unavailable")
    report: ThinReport = ThinReport()
    stop: StopReason | None = None
