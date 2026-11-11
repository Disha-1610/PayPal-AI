"""Global limits (rule: any iteration cap or repeated progress hash stops the task and reports).

Checked each time the search loop is (re)entered. Pure function of the persisted counters, so a resumed
run reaches the same decision.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.schemas.state import TaskState


class StopCause(StrEnum):
    ITERATION_CAP = "iteration_cap"
    NO_PROGRESS = "no_progress"


class LimitRoute(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


@dataclass(frozen=True)
class GraphLimits:
    max_cycles: int = 10  # entries into the search loop per task


_ROUTES: dict[StopCause | None, LimitRoute] = {
    None: LimitRoute.CONTINUE,
    StopCause.ITERATION_CAP: LimitRoute.STOP,
    StopCause.NO_PROGRESS: LimitRoute.STOP,
}


def breached(state: TaskState, limits: GraphLimits) -> StopCause | None:
    """``cycle_hashes[-1]`` already seen earlier means this cycle starts from exactly where one ended before."""
    hashes = state.get("cycle_hashes", [])
    if state.get("cycles", 0) > limits.max_cycles:
        return StopCause.ITERATION_CAP
    if hashes and hashes[-1] in hashes[:-1]:
        return StopCause.NO_PROGRESS
    return None


def route_limits(state: TaskState, limits: GraphLimits) -> LimitRoute:
    return _ROUTES[breached(state, limits)]
