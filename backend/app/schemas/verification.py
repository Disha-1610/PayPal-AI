"""Verification: did the counterparty deliver what the user approved?

Domain-independent. A domain supplies *evidence* (merchant order status, submitted milestone, agent
execution log) already mapped to ``ObservedTerms`` plus a ``confirmed`` flag; the verifier compares it
with the approved terms deterministically. Settlement (capture) is decided from this verdict only.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.recheck import MismatchReport, ObservedTerms


class VerificationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"  # wait/retry within limits, then give up


class VerificationSpec(BaseModel):
    """Declarative, per-domain verification settings (data only, like the rest of ``DomainPlugin``)."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=5, ge=1, description="Evidence polls before an unresolved payment is voided")
    tolerated_fields: frozenset[str] = Field(
        default=frozenset(), description="Dotted fields whose drift is harmless (see MismatchPolicy)"
    )


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str = Field(min_length=1, description="Where the evidence came from (connector id, evaluator)")
    observed: ObservedTerms
    confirmed: bool | None = Field(
        default=None, description="True = counterparty/evaluator confirmed fulfilment; False = rejected; None = pending"
    )


class VerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: VerificationVerdict
    reasons: tuple[str, ...] = ()
    report: MismatchReport
