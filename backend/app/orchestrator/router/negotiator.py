"""Deterministic routing after the Negotiator. A pure function of the structured result.

An accepted deal goes back through Executor and Authorize; if its terms hash changed the user must approve
the new hash first. A counter is sent to the counterparty. Anything else stops and reports.
"""

from app.audit.events import AuditScope, Phase
from app.schemas.negotiator import (
    NegotiationOutcome,
    NegotiationResult,
    NegotiatorRoute,
)


def route_negotiation(result: NegotiationResult, *, audit: AuditScope | None = None) -> NegotiatorRoute:
    match result.outcome:
        case NegotiationOutcome.ACCEPT:
            route = NegotiatorRoute.ASK_USER_APPROVAL if result.needs_fresh_approval else NegotiatorRoute.EXECUTE
        case NegotiationOutcome.COUNTER:
            route = NegotiatorRoute.SEND_COUNTER
        case NegotiationOutcome.STOP:
            route = NegotiatorRoute.REPORT
    if audit:
        audit.emit(
            Phase.EXECUTION, "NEGOTIATION_ROUTED", node="router", decision=route.value,
            metadata={"outcome": result.outcome.value, "round": result.round, "needs_fresh_approval": result.needs_fresh_approval},
        )
    return route
