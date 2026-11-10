"""Negotiator models: the sanitized counter-offer, the user-approved envelope, strict LLM output and the result.

Counter-offers are structured fields only. Free text from a contractor or A2A peer is stripped before this
point (``parse_counter_offer``), so the Negotiator prompt and the Coordinator never see untrusted prose.
The model never routes: it returns an action enum plus numbers; the Guard checks them and the Router
reads ``NegotiationResult``.
"""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.hashing import stable_hash
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.verification import VerificationVerdict

_TOKEN = re.compile(r"^[a-z0-9_.:-]{1,64}$")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware")
    return value


def _check_terms(terms: dict[str, str]) -> dict[str, str]:
    """Terms are machine tokens (``warranty: 12m``), never sentences: prose cannot ride along."""
    if len(terms) > 32:
        raise ValueError("too many terms")
    for key, value in terms.items():
        if not _TOKEN.match(key) or not _TOKEN.match(value):
            raise ValueError(f"term {key!r} must be lowercase tokens, not free text")
    return terms


class CounterMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_minor: int = Field(gt=0)
    due_date: datetime
    criteria_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _utc = field_validator("due_date")(_aware)


def milestones_hash(milestones: tuple[CounterMilestone, ...]) -> str:
    return stable_hash(
        [
            {"amount_minor": m.amount_minor, "due_date": m.due_date.astimezone(UTC).isoformat(), "criteria_hash": m.criteria_hash}
            for m in milestones
        ]
    )


class _OfferBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    price_minor: int = Field(ge=0)
    delivery_due: datetime
    milestones: tuple[CounterMilestone, ...] = ()
    terms: dict[str, str] = Field(default_factory=dict)

    _utc = field_validator("delivery_due")(_aware)
    _tokens = field_validator("terms")(_check_terms)

    @model_validator(mode="after")
    def _milestones_add_up(self) -> Self:
        if self.milestones and sum(m.amount_minor for m in self.milestones) != self.price_minor:
            raise ValueError("milestone amounts must add up to the price")
        return self


class CounterOffer(_OfferBody):
    """A complete offer on the table (the counterparty's, or ours once the identity is filled in)."""

    subject_id: str = Field(min_length=1, max_length=128)
    counterparty_id: str = Field(min_length=1, max_length=128)
    currency: str = Field(min_length=3, max_length=3)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class ProposedOffer(_OfferBody):
    """What the model may propose. It has no item, seller or currency field, so it cannot change them."""


class NegotiationTrigger(StrEnum):
    VERIFIER_FAIL = "verifier_fail"
    COUNTER_OFFER = "counter_offer"


def negotiation_trigger(verdict: VerificationVerdict, *, escalate_unresolved: bool) -> NegotiationTrigger | None:
    """Only a contractor/A2A Verifier FAIL opens a negotiation; a counter-offer is the other trigger."""
    return NegotiationTrigger.VERIFIER_FAIL if verdict is VerificationVerdict.FAIL and escalate_unresolved else None


class NegotiationEnvelope(BaseModel):
    """What the user approved. ``baseline_terms`` are the signed terms; the rest bound any proposal."""

    model_config = ConfigDict(frozen=True)

    baseline_terms: ApprovalTerms
    baseline_offer: CounterOffer = Field(description="The approved deal, in offer form")
    max_total_minor: int = Field(ge=0, description="Max budget")
    latest_acceptable_at: datetime
    non_negotiable_terms: dict[str, str] = Field(default_factory=dict)
    seller_party: str = Field(default="payee_id", description="Key of the seller in baseline_terms.parties")

    _utc = field_validator("latest_acceptable_at")(_aware)
    _tokens = field_validator("non_negotiable_terms")(_check_terms)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.baseline_offer.currency != self.baseline_terms.currency:
            raise ValueError("baseline offer and terms use different currencies")
        if self.seller_party not in self.baseline_terms.parties:
            raise ValueError(f"baseline terms have no party {self.seller_party!r}")
        return self


class NegotiationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trigger: NegotiationTrigger
    round: int = Field(ge=1, description="1-based round about to be played")
    incoming: CounterOffer | None = None
    failed_criteria: tuple[int, ...] = Field(default=(), description="Criterion indexes only, never text")

    @model_validator(mode="after")
    def _incoming_matches_trigger(self) -> Self:
        if self.trigger is NegotiationTrigger.COUNTER_OFFER and self.incoming is None:
            raise ValueError("a counter-offer trigger needs the incoming offer")
        return self


class NegotiatorAction(StrEnum):
    ACCEPT = "accept"  # accept the incoming offer as it stands
    COUNTER = "counter"
    STOP = "stop"


class NegotiatorDecision(BaseModel):
    """Strict LLM output. ``rationale`` is audit-only and never routed on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: NegotiatorAction
    counter: ProposedOffer | None = None
    rationale: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _counter_iff_counter(self) -> Self:
        if (self.action is NegotiatorAction.COUNTER) != (self.counter is not None):
            raise ValueError("counter must be present exactly when action is 'counter'")
        return self


class NegotiatorPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_rounds: int = Field(default=3, ge=1, le=5, description="Rounds before the agent must accept or stop")
    parse_retries: int = Field(default=1, ge=0)
    decision_attempts: int = Field(default=2, ge=1, description="Tries per call when the Guard or a check rejects")


class Violation(StrEnum):
    CURRENCY_CHANGED = "currency_changed"
    OVER_BUDGET = "over_budget"
    PAST_DEADLINE = "past_deadline"
    NON_NEGOTIABLE_TERM = "non_negotiable_term"


class ProposalDecision(StrEnum):
    ALLOWED = "allowed"  # identical to the approved terms hash: the existing approval still holds
    REQUIRES_APPROVAL = "requires_approval"  # inside the envelope but the hash changed: fresh user approval
    BLOCKED = "blocked"  # outside the envelope: never proposed, never accepted


class GuardResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: ProposalDecision
    violations: tuple[Violation, ...] = ()
    changed_fields: tuple[str, ...] = ()
    terms_hash: str


class NegotiationOutcome(StrEnum):
    ACCEPT = "accept"
    COUNTER = "counter"
    STOP = "stop"


class StopReason(StrEnum):
    ROUND_CAP = "round_cap"
    MODEL_STOPPED = "model_stopped"
    MODEL_UNAVAILABLE = "model_unavailable"


class NegotiatorRoute(StrEnum):
    EXECUTE = "execute"  # accepted, approval still valid: Executor -> Authorize
    ASK_USER_APPROVAL = "ask_user_approval"  # accepted, terms changed: fresh approval, then Executor
    SEND_COUNTER = "send_counter"  # Guard-checked counter goes to the counterparty
    REPORT = "report"  # stop and report to the user


class NegotiationResult(BaseModel):
    """What the Router reads."""

    model_config = ConfigDict(frozen=True)

    outcome: NegotiationOutcome
    round: int
    offer: CounterOffer | None = None
    guard: GuardResult | None = None
    stop_reason: StopReason | None = None
    reasons: tuple[str, ...] = ()

    @property
    def needs_fresh_approval(self) -> bool:
        return self.guard is not None and self.guard.decision is ProposalDecision.REQUIRES_APPROVAL
