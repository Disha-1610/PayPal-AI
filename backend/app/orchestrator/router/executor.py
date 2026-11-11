"""Deterministic routing after the Executor's recheck. A pure function of the ``MismatchReport``.

Nothing proceeds to PayPal authorization unless the live terms still match the approved ones. Any
price/delivery/availability change either goes back to search or to the user for a fresh approval.
"""

from enum import StrEnum

from app.audit.events import AuditScope, Phase
from app.schemas.recheck import MismatchAction, MismatchReport


class ExecutorRoute(StrEnum):
    AUTHORIZE = "authorize"  # terms unchanged: on to PayPal authorize
    EVALUATE = "evaluate"  # subject unavailable: back to the Evaluator for another candidate
    ADAPT = "adapt"  # delivery/deadline can no longer be met
    ASK_USER = "ask_user"  # approval hash would mismatch, or live data unknown: ask again, with the diff


_ROUTES = {
    MismatchAction.PROCEED: ExecutorRoute.AUTHORIZE,
    MismatchAction.INSUFFICIENT_DATA: ExecutorRoute.ASK_USER,
    MismatchAction.DEADLINE_MISSED: ExecutorRoute.ADAPT,
    MismatchAction.REAPPROVE: ExecutorRoute.ASK_USER,
    MismatchAction.UNAVAILABLE: ExecutorRoute.EVALUATE,
}


def route_execution(report: MismatchReport, *, audit: AuditScope | None = None) -> ExecutorRoute:
    route = _ROUTES[report.action]
    if audit:
        audit.emit(
            Phase.EXECUTION, "EXECUTION_ROUTED", node="router", decision=route.value,
            metadata={"action": report.action.name, "changes": [c.field for c in report.changes]},
        )
    return route
