"""Deterministic routing after the Coordinator. The Router executes; the Coordinator only chooses."""

from enum import StrEnum

from app.orchestrator.agents.coordinator.agent import CoordinatorResult
from app.schemas.coordinator import Action


class CoordinatorRoute(StrEnum):
    SEARCH = "search"  # re-run discovery / evaluation (plan steps were re-queued)
    SWITCH_CANDIDATE = "switch_candidate"
    ASK_USER = "ask_user"
    STOP = "stop"  # iteration cap or no progress: stop and report


_ROUTES = {
    Action.WIDEN: CoordinatorRoute.SEARCH,
    Action.REPLAN: CoordinatorRoute.SEARCH,
    Action.SWITCH_CANDIDATE: CoordinatorRoute.SWITCH_CANDIDATE,
    Action.ASK_USER: CoordinatorRoute.ASK_USER,
    Action.ESCALATE: CoordinatorRoute.ASK_USER,
    Action.STOP: CoordinatorRoute.STOP,
}


def route_coordinator_result(result: CoordinatorResult) -> CoordinatorRoute:
    return _ROUTES[result.action]
