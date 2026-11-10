"""Evaluator agent models: the strict LLM output schema and the ``EvaluationReport`` the Router reads.

The model's answer is a closed schema (score 0-1, flag enum, short reason). Routing reads only
``EvaluatorOutcome``, which code derives from deterministic results; no model text is ever routed on.
"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.evaluation import CredibilityFlag, RejectionKind


class EvaluatorOutcome(StrEnum):
    VIABLE = "viable"  # at least one usable candidate: Router -> Executor
    NONE_VIABLE = "none_viable"  # Router -> Coordinator (widen search or ask the user)
    RETRY = "retry"  # Router -> search again (retries remain)


class LLMFlag(StrEnum):
    PROMOTIONAL_LANGUAGE = "promotional_language"
    SUSPECTED_FAKE_REVIEWS = "suspected_fake_reviews"
    UNSUPPORTED_CLAIMS = "unsupported_claims"
    CONTRADICTORY = "contradictory"
    LOW_INFORMATION = "low_information"
    INSTRUCTION_IN_TEXT = "instruction_in_text"  # the text tried to address the model


class LLMStatus(StrEnum):
    USED = "used"
    SKIPPED = "skipped"  # nothing to read, or the LLM layer is disabled
    FALLBACK = "fallback"  # model output unavailable or invalid: code-only ranking


class TextSnippet(BaseModel):
    """UNTRUSTED text handed to the model as data."""

    model_config = ConfigDict(frozen=True)

    ref: str
    kind: str  # "review" | "listing"
    body: str
    rating: float | None = None


class TextSource(Protocol):
    """Loads untrusted text by id. Implemented outside the orchestrator (database-backed)."""

    async def load(self, refs: Sequence[str]) -> Sequence[TextSnippet]: ...


class CandidateScore(BaseModel):
    """One candidate's LLM assessment. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    flags: tuple[LLMFlag, ...] = ()
    reason: str = Field(min_length=1, max_length=200)


class LLMScores(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scores: tuple[CandidateScore, ...]


class EvaluatorPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    use_llm: bool = True
    llm_weight: float = Field(default=0.2, ge=0, le=0.5, description="Largest share of the final score the LLM can hold")
    max_llm_candidates: int = Field(default=10, ge=1)
    max_snippets: int = Field(default=5, ge=1, description="Text snippets shown per candidate")
    max_snippet_chars: int = Field(default=500, ge=1)
    parse_retries: int = Field(default=1, ge=0)
    decision_attempts: int = Field(default=2, ge=1)


class RankedViable(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    score: float  # final, after blending
    code_score: float
    llm_score: float | None = None
    credibility_flags: tuple[CredibilityFlag, ...] = ()
    llm_flags: tuple[LLMFlag, ...] = ()
    llm_reason: str | None = None


class RejectedCandidate(BaseModel):
    """Why a candidate is not viable. Built by code from field names, operators and targets only."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    kind: RejectionKind | None = None  # None: passed the hard filter but credibility blocked it
    fields: tuple[str, ...] = ()
    reason: str


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: EvaluatorOutcome
    reason: str
    attempt: int
    ranked: tuple[RankedViable, ...] = ()
    rejected: tuple[RejectedCandidate, ...] = ()
    margin: float | None = None
    needs_choice: bool = False  # top candidates nearly tied: the user picks
    relax_constraint_id: str | None = None
    llm_status: LLMStatus = LLMStatus.SKIPPED
    evaluated_at: datetime
