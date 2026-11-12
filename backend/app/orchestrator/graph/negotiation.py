"""Negotiation nodes for the task graph. Pure functions over state; ``builder`` only wires them to edges.

    settle (verification failed) -> guard_negotiate -> negotiate -> route_negotiation:
        accept          -> request_approval (the user approves the exact deal, EXECUTE included, see below)
        counter         -> send_counter -> await_counterparty -> guard_negotiate -> negotiate ...
        stop / invalid  -> stop_report

The model never routes and never sees counterparty prose: offers are sanitized into structured fields before they
reach state (``parse_counter_offer``). An accepted deal always goes back to the user, even when its terms hash is
unchanged: the earlier approval was single-use and already spent on a payment that has since been voided, so
re-using it would be refused by the Payment Service.
"""

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from app.audit.events import AuditScope, Phase
from app.llm.providers import LLMProvider
from app.orchestrator.agents.negotiator.agent import negotiate
from app.orchestrator.agents.negotiator.offer import parse_counter_offer
from app.orchestrator.guard.negotiation_guard import proposed_terms
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, PendingApproval
from app.schemas.negotiator import (
    CounterOffer,
    NegotiationEnvelope,
    NegotiationOutcome,
    NegotiationRequest,
    NegotiationTrigger,
    NegotiatorPolicy,
)
from app.schemas.state import TaskState

# Builds the user-approved negotiation bounds from the approved terms (per domain: contractor milestone, A2A deal).
EnvelopeFor = Callable[[ApprovalTerms, TaskState], NegotiationEnvelope]

STATUS_NEGOTIATING = "negotiating"
STATUS_DECLINED = "negotiation_declined"
STATUS_INVALID = "negotiation_invalid"
STATUS_UNAVAILABLE = "negotiation_unavailable"


class ChannelError(Exception):
    """A counterparty channel could not deliver. The message is safe to show the user."""


class CounterpartyChannel(Protocol):
    """Delivers a Guard-checked counter-offer to the other side. Replies come back through
    ``RunController.receive_counter_offer``; sending never decides anything."""

    async def send_counter(self, run_id: str, offer: CounterOffer) -> None: ...


async def negotiate_step(
    state: TaskState, *, envelope_for: EnvelopeFor, policy: NegotiatorPolicy, provider: LLMProvider | None, audit: AuditScope | None
) -> dict[str, object]:
    """One negotiation round. An accepted offer becomes the terms put to the user (``pending_approval``)."""
    approved = state["approvals"][-1]
    assert approved.terms is not None  # guard_negotiate guarantees it
    incoming = state.get("incoming_offer")
    request = NegotiationRequest(
        trigger=NegotiationTrigger.COUNTER_OFFER if incoming else NegotiationTrigger.VERIFIER_FAIL,
        round=state.get("negotiation_round", 0) + 1, incoming=incoming,
    )
    envelope = envelope_for(approved.terms, state)
    result = await negotiate(envelope, request, policy=policy, provider=provider, audit=audit)
    update: dict[str, object] = {
        "negotiation_round": request.round, "negotiation_result": result, "incoming_offer": None, "status": STATUS_NEGOTIATING,
    }
    if result.outcome is NegotiationOutcome.ACCEPT and result.offer is not None:
        terms = proposed_terms(envelope, result.offer)
        update["agreed_terms"] = terms
        update["pending_approval"] = PendingApproval(
            action=Action.CREATE_ORDER, candidate_id=approved.candidate_id, platform=approved.platform, terms=terms,
            terms_hash=terms.terms_hash(),
        )
    return update


async def send_counter_step(
    state: TaskState, *, channel: CounterpartyChannel | None, audit: AuditScope | None
) -> dict[str, object]:
    offer = state["negotiation_result"].offer
    if channel is None or offer is None:
        return {"status": STATUS_UNAVAILABLE, "errors": ["no channel to send the counter-offer to the counterparty"]}
    try:
        await channel.send_counter(state["run_id"], offer)
    except ChannelError as exc:
        return {"status": STATUS_UNAVAILABLE, "errors": [f"could not send the counter-offer: {exc}"]}
    if audit:
        audit.emit(
            Phase.EXECUTION, "COUNTER_OFFER_SENT", node="negotiation", decision="sent",
            metadata={"round": state.get("negotiation_round", 0), "price_minor": offer.price_minor},
        )
    return {"status": "awaiting_counterparty"}


def counterparty_reply(raw: Any, *, audit: AuditScope | None) -> dict[str, object]:
    """The resume value of ``await_counterparty``: a counter-offer, or a decline. Anything malformed rejects the
    whole reply (it is never partially trusted)."""
    if isinstance(raw, Mapping) and raw.get("decision") == "timeout":
        return {"status": STATUS_DECLINED, "errors": ["the counterparty did not reply to the counter-offer"]}
    if not isinstance(raw, Mapping) or raw.get("decision") == "decline":
        return {"status": STATUS_DECLINED}
    try:
        offer = parse_counter_offer(raw)
    except ValidationError:
        if audit:
            audit.emit(Phase.EXECUTION, "COUNTER_OFFER_REJECTED", node="negotiation", decision="invalid", reason="malformed offer")
        return {"status": STATUS_INVALID, "errors": ["the counterparty's offer was malformed and was rejected"]}
    return {"incoming_offer": offer, "status": STATUS_NEGOTIATING}
