"""Verifier agent models: the strict LLM output schemas and the result the Router reads.

The model never returns a verdict. It returns a per-criterion status with cited evidence ids; code
reduces those to ``VerificationVerdict`` and then to ``Evidence.confirmed``. Free text (``note``,
``challenges``) is audit-only and never routed on.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.verification import VerificationVerdict


class CriterionStatus(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    UNCLEAR = "unclear"


class DeliverableItem(BaseModel):
    """One piece of UNTRUSTED deliverable evidence (diff, report, file listing) shown to the model as data."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=32)
    content: str


class CriterionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_index: int = Field(ge=0)
    status: CriterionStatus
    evidence_ids: tuple[str, ...] = ()
    note: str = Field(min_length=1, max_length=200)


class GeneratorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[CriterionFinding, ...]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class CriticOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[CriterionFinding, ...]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    challenges: tuple[str, ...] = Field(default=(), max_length=10)


class VerifierPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_passes: int = Field(default=3, ge=1, le=3, description="Generator/critic rounds before escalating")
    min_confidence: float = Field(default=0.7, ge=0, le=1, description="Both roles must reach this for a verdict")
    parse_retries: int = Field(default=1, ge=0)
    decision_attempts: int = Field(default=2, ge=1, description="Tries per model call when a deterministic check rejects")
    max_item_chars: int = Field(default=4000, ge=1)


class VerifierRoute(StrEnum):
    CAPTURE = "capture"
    VOID_THEN_DISCOVERY = "void_then_discovery"  # shopping: release the hold and search again
    VOID = "void"  # contractor: release the hold; the next step is the Coordinator's
    WAIT = "wait"  # shopping: the Monitor or the deadline resolves it
    ESCALATE_HUMAN = "escalate_human"  # contractor: a person decides


class DeliverableVerdict(BaseModel):
    """What the Router reads. ``confirmed`` is the only value that flows into ``Evidence``."""

    model_config = ConfigDict(frozen=True)

    verdict: VerificationVerdict
    passes: int
    reasons: tuple[str, ...] = ()
    findings: tuple[CriterionFinding, ...] = ()

    @property
    def confirmed(self) -> bool | None:
        match self.verdict:
            case VerificationVerdict.PASS:
                return True
            case VerificationVerdict.FAIL:
                return False
            case _:
                return None
