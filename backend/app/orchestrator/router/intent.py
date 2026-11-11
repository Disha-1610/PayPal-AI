"""Deterministic routing after the Intent agent (full extraction or a diff). A pure function of the status."""

from enum import StrEnum

from app.audit.events import AuditScope, Phase
from app.schemas.intent import IntentResult, IntentStatus


class IntentRoute(StrEnum):
    DISCOVERY = "discovery"  # clear: constraints are complete and consistent
    ASK_USER = "ask_user"  # blocking ambiguity or conflict: interrupt() for an answer
    REPORT = "report"  # out of scope: stop and report


_ROUTES = {
    IntentStatus.READY: IntentRoute.DISCOVERY,
    IntentStatus.BLOCKED: IntentRoute.ASK_USER,
    IntentStatus.OUT_OF_SCOPE: IntentRoute.REPORT,
}


def route_intent(result: IntentResult, *, audit: AuditScope | None = None) -> IntentRoute:
    route = _ROUTES[result.status]
    if audit:
        audit.emit(
            Phase.INTENT, "INTENT_ROUTED", node="router", decision=route.value,
            metadata={"status": result.status.value, "missing": list(result.missing), "conflicts": list(result.conflicts)},
        )
    return route
