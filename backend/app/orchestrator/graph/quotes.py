"""Quote nodes for a counterparty the user named (contractor / A2A). Pure functions over state; ``builder`` wires them.

    discovery_start -> request_quote -> await_quote (interrupt: hours or days) -> guard_evaluate -> evaluate ...
                                    \\-> counterparty_followup (declined / no reply / quote unusable)
    counterparty_followup (asks the user) -> WAIT: request_quote again | new contact: request_quote | CANCEL: stop

The run is parked at ``await_quote`` in the checkpoint; nothing is kept in memory while it waits. A reply resumes it
through ``RunController`` with a payload that is sanitized here (``parse_quote``); prose never reaches state.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError

from app.audit.events import AuditScope, Phase
from app.orchestrator.graph.negotiation import ChannelError
from app.schemas.counterparty import (
    CounterpartyContact,
    contact_from_text,
    parse_quote,
    quote_candidate,
)
from app.schemas.state import TaskState

STATUS_AWAITING_QUOTE = "awaiting_quote"
STATUS_QUOTED = "discovered"  # the quote is the candidate pool: evaluation proceeds as after a search
STATUS_FOLLOWUP = "counterparty_followup"
STATUS_REQUOTE = "requote"
STATUS_FOLLOWUP_INVALID = "followup_invalid"

_WAIT = re.compile(r"^\s*(wait|keep waiting|remind|again|resend|retry)\b", re.IGNORECASE)
_CANCEL = re.compile(r"^\s*(cancel|stop|give up|no)\b", re.IGNORECASE)


@dataclass(frozen=True)
class QuoteRequest:
    run_id: str
    contact: CounterpartyContact
    goal: str
    currency: str
    deliver_by: datetime | None
    round: int


class QuoteChannel(Protocol):
    """Sends a quote request to the counterparty; the reply resumes ``await_quote`` later. Raises on failure."""

    async def request_quote(self, request: QuoteRequest) -> None: ...


def uses_quotes(state: TaskState, channel: QuoteChannel | None) -> bool:
    return channel is not None and state.get("counterparty") is not None


async def request_quote_step(state: TaskState, *, channel: QuoteChannel, audit: AuditScope | None) -> dict[str, object]:
    contact = state["counterparty"]
    assert contact is not None  # routed here only when a counterparty is on record
    round_ = state.get("quote_round", 0) + 1
    request = QuoteRequest(
        run_id=state["run_id"], contact=contact, goal=state.get("goal") or state["original_intent"],
        currency=state["authorization"].currency, deliver_by=state.get("deliver_by"), round=round_,
    )
    try:
        await channel.request_quote(request)
    except ChannelError as exc:
        if audit:
            audit.emit(Phase.DISCOVERY, "QUOTE_REQUEST_FAILED", node="request_quote", decision="failed", reason=str(exc)[:200])
        return {"quote_round": round_, "status": STATUS_FOLLOWUP, "errors": [f"could not reach the counterparty: {exc}"]}
    if audit:
        audit.emit(Phase.DISCOVERY, "QUOTE_REQUESTED", node="request_quote", decision="sent", metadata={"round": round_})
    return {"quote_round": round_, "status": STATUS_AWAITING_QUOTE}


def quote_reply(raw: Any, state: TaskState, *, audit: AuditScope | None) -> dict[str, object]:
    """The resume value of ``await_quote``: a quote, a decline or the sweeper's timeout."""
    contact = state["counterparty"]
    assert contact is not None
    if isinstance(raw, Mapping) and raw.get("decision") == "timeout":
        return {"status": STATUS_FOLLOWUP, "errors": [f"{contact.name} has not replied"]}
    if not isinstance(raw, Mapping):
        return {"status": STATUS_FOLLOWUP, "errors": ["the counterparty's reply was unreadable"]}
    try:
        quote = parse_quote(raw)
    except ValidationError:
        if audit:
            audit.emit(Phase.DISCOVERY, "QUOTE_REJECTED", node="await_quote", decision="invalid", reason="malformed quote")
        return {"status": STATUS_FOLLOWUP, "errors": [f"{contact.name}'s quote was malformed and was rejected"]}
    if quote is None:
        return {"status": STATUS_FOLLOWUP, "errors": [f"{contact.name} declined the request"]}
    if quote.currency != state["authorization"].currency:
        return {"status": STATUS_FOLLOWUP, "errors": [f"{contact.name} quoted in {quote.currency}, not {state['authorization'].currency}"]}
    candidate = quote_candidate(
        contact, quote, run_id=state["run_id"], domain=state.get("domain", ""), goal=state.get("goal") or "the requested work"
    )
    if audit:
        audit.emit(Phase.DISCOVERY, "QUOTE_RECEIVED", node="await_quote", decision="quoted", metadata={"price_minor": quote.price_minor})
    return {"candidates": [candidate], "status": STATUS_QUOTED}


def followup_question(state: TaskState) -> str:
    contact = state.get("counterparty")
    name = contact.name if contact is not None else "The counterparty"
    errors = state.get("errors") or []
    reason = errors[-1] if errors else f"{name} could not give a usable quote"
    return (
        f"{reason[0].upper()}{reason[1:]}. Reply WAIT to ask {name} again, send another contractor's or agent's email, "
        "phone number (+country code) or https address to ask them instead, or CANCEL to stop."
    )


def followup_answer(raw: Any, state: TaskState) -> dict[str, object]:
    text = (str(raw.get("text", "")) if isinstance(raw, Mapping) else str(raw or "")).strip()
    if _CANCEL.match(text):
        return {"status": "cancelled", "stop_reason": "cancelled by the user while waiting for the counterparty"}
    current = state.get("counterparty")
    replacement = contact_from_text(text)
    if replacement is not None:
        domain = state.get("domain", "")
        if domain == "a2a" and replacement.agent_endpoint is None and replacement.website is not None:
            replacement = replacement.model_copy(update={"agent_endpoint": replacement.website})
        try:
            replacement.require_for(domain)
        except ValueError as exc:
            return {"status": STATUS_FOLLOWUP_INVALID, "errors": [str(exc)]}
        return {"counterparty": replacement, "candidates": [], "status": STATUS_REQUOTE}
    if _WAIT.match(text) and current is not None:
        return {"status": STATUS_REQUOTE}
    return {"status": STATUS_FOLLOWUP_INVALID, "errors": ["reply WAIT, CANCEL, or the new counterparty's contact details"]}


__all__ = [
    "STATUS_AWAITING_QUOTE", "STATUS_FOLLOWUP", "STATUS_FOLLOWUP_INVALID", "STATUS_QUOTED", "STATUS_REQUOTE",
    "QuoteChannel", "QuoteRequest", "followup_answer", "followup_question", "quote_reply", "request_quote_step", "uses_quotes",
]
