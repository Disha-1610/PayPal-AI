"""Sanitize a raw counter-offer (contractor / A2A payload) into the strict structured model.

Only known fields survive; every other key, including any prose, is dropped. Terms must be machine tokens
(see ``CounterOffer``), so free text cannot be smuggled through a field either. Invalid data raises
``ValidationError`` and the offer is rejected as a whole.
"""

from collections.abc import Mapping
from datetime import datetime

from app.schemas.approval_terms import ApprovalTerms
from app.schemas.negotiator import CounterMilestone, CounterOffer, NegotiationEnvelope


def _pick(raw: Mapping[str, object], model: type[CounterOffer] | type[CounterMilestone]) -> dict[str, object]:
    return {name: raw[name] for name in model.model_fields if name in raw}


def parse_counter_offer(raw: Mapping[str, object]) -> CounterOffer:
    fields = _pick(raw, CounterOffer)
    milestones = fields.get("milestones")
    if isinstance(milestones, list | tuple):
        fields["milestones"] = [_pick(m, CounterMilestone) if isinstance(m, Mapping) else m for m in milestones]
    return CounterOffer.model_validate(fields)


def envelope_from_terms(
    terms: ApprovalTerms,
    *,
    max_total_minor: int | None = None,
    latest_acceptable_at: datetime | None = None,
    non_negotiable_terms: Mapping[str, str] | None = None,
    baseline_terms: Mapping[str, str] | None = None,
    seller_party: str = "payee_id",
) -> NegotiationEnvelope:
    """Build the user-approved negotiation envelope from the signed ``ApprovalTerms``.

    The baseline offer mirrors the approved terms exactly, so an unchanged offer hashes to the approved
    hash. Bounds default to the approved ceiling and deadline: a negotiation can never silently raise
    the budget or move the deadline; a wider bound must be passed explicitly from the user's constraints
    and may not sit below the approved terms.
    """
    price = terms.expected_total_minor if terms.expected_total_minor is not None else terms.max_total_minor
    deadline = latest_acceptable_at or terms.latest_acceptable_at
    if deadline is None:
        raise ValueError("terms have no deadline: pass latest_acceptable_at")
    ceiling = terms.max_total_minor if max_total_minor is None else max_total_minor
    if ceiling < price:
        raise ValueError("max_total_minor is below the approved price")
    if terms.latest_acceptable_at is not None and deadline < terms.latest_acceptable_at:
        raise ValueError("latest_acceptable_at is before the approved delivery date")
    baseline = CounterOffer(
        subject_id=terms.subject_id, counterparty_id=terms.parties[seller_party], currency=terms.currency,
        price_minor=price, delivery_due=terms.latest_acceptable_at or deadline, terms=dict(baseline_terms or {}),
    )
    return NegotiationEnvelope(
        baseline_terms=terms, baseline_offer=baseline, max_total_minor=ceiling, latest_acceptable_at=deadline,
        non_negotiable_terms=dict(non_negotiable_terms or {}), seller_party=seller_party,
    )
