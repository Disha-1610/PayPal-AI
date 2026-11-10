"""Deterministic routing after the Evaluator. A pure function of ``EvaluationReport.outcome``."""

from enum import StrEnum

from app.audit.events import AuditScope, Phase
from app.schemas.evaluator import EvaluationReport, EvaluatorOutcome


class EvaluatorRoute(StrEnum):
    EXECUTE = "execute"  # viable candidates: on to the Executor (through the Guard)
    SEARCH = "search"  # retry: back to discovery
    COORDINATOR = "coordinator"  # none viable: the Coordinator decides widen vs ask the user


_ROUTES = {
    EvaluatorOutcome.VIABLE: EvaluatorRoute.EXECUTE,
    EvaluatorOutcome.RETRY: EvaluatorRoute.SEARCH,
    EvaluatorOutcome.NONE_VIABLE: EvaluatorRoute.COORDINATOR,
}


def route_evaluation(report: EvaluationReport, *, audit: AuditScope | None = None) -> EvaluatorRoute:
    route = _ROUTES[report.outcome]
    if audit:
        audit.emit(
            Phase.EVALUATION, "EVALUATION_ROUTED", node="router", decision=route.value,
            metadata={"outcome": report.outcome.value, "attempt": report.attempt, "needs_choice": report.needs_choice},
        )
    return route
