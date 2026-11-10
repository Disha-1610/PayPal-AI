"""Deterministic Guard for negotiation proposals.

Every offer the Negotiator would send or accept is turned into ``ApprovalTerms`` and compared with the
approved terms by hash. Outside the envelope (budget, deadline, non-negotiable terms, currency) -> BLOCKED.
Inside it but with any changed item, seller, price, delivery, milestones or terms -> the hash differs, so a
fresh user approval is required: an approval is valid only for its exact hash.
"""

from app.core.hashing import stable_hash
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.negotiator import (
    CounterOffer,
    GuardResult,
    NegotiationEnvelope,
    ProposalDecision,
    Violation,
    milestones_hash,
)


def _changed_fields(base: CounterOffer, offer: CounterOffer) -> tuple[str, ...]:
    pairs = {
        "item": (base.subject_id, offer.subject_id),
        "seller": (base.counterparty_id, offer.counterparty_id),
        "currency": (base.currency, offer.currency),
        "price": (base.price_minor, offer.price_minor),
        "delivery": (base.delivery_due, offer.delivery_due),
        "milestones": (milestones_hash(base.milestones), milestones_hash(offer.milestones)),
        "terms": (base.terms, offer.terms),
    }
    return tuple(name for name, (before, after) in pairs.items() if before != after)


def proposed_terms(envelope: NegotiationEnvelope, offer: CounterOffer) -> ApprovalTerms:
    """The approved terms with only the fields the offer actually changes replaced."""
    base, terms = envelope.baseline_offer, envelope.baseline_terms
    changed = set(_changed_fields(base, offer))
    update: dict[str, object] = {}
    if "item" in changed:
        update["subject_id"] = offer.subject_id
    if "seller" in changed:
        update["parties"] = {**terms.parties, envelope.seller_party: offer.counterparty_id}
    if "currency" in changed:
        update["currency"] = offer.currency
    if "price" in changed:
        update["max_total_minor"] = update["expected_total_minor"] = offer.price_minor
    if "delivery" in changed:
        update["latest_acceptable_at"] = offer.delivery_due
    domain_data = dict(terms.domain_data)
    if "milestones" in changed:
        domain_data["milestones_hash"] = milestones_hash(offer.milestones)
    if "terms" in changed:
        domain_data["terms_hash"] = stable_hash(sorted(offer.terms.items()))
    update["domain_data"] = domain_data
    return terms.model_copy(update=update)


def _violations(envelope: NegotiationEnvelope, offer: CounterOffer) -> tuple[Violation, ...]:
    found: list[Violation] = []
    if offer.currency != envelope.baseline_terms.currency:
        found.append(Violation.CURRENCY_CHANGED)
    if offer.price_minor > envelope.max_total_minor:
        found.append(Violation.OVER_BUDGET)
    latest = envelope.latest_acceptable_at
    if offer.delivery_due > latest or any(m.due_date > latest for m in offer.milestones):
        found.append(Violation.PAST_DEADLINE)
    if any(offer.terms.get(key) != value for key, value in envelope.non_negotiable_terms.items()):
        found.append(Violation.NON_NEGOTIABLE_TERM)
    return tuple(found)


def check_proposal(envelope: NegotiationEnvelope, offer: CounterOffer) -> GuardResult:
    terms = proposed_terms(envelope, offer)
    terms_hash = terms.terms_hash()
    changed = _changed_fields(envelope.baseline_offer, offer)
    violations = _violations(envelope, offer)
    if violations:
        decision = ProposalDecision.BLOCKED
    elif terms_hash != envelope.baseline_terms.terms_hash():
        decision = ProposalDecision.REQUIRES_APPROVAL
    else:
        decision = ProposalDecision.ALLOWED
    return GuardResult(decision=decision, violations=violations, changed_fields=changed, terms_hash=terms_hash)
