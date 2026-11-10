"""Deterministic routing after the Monitor. Ordinary routing never needs an LLM.

The router only reads the structured ``MonitorResult``. A deadline problem never edits the user's
deadline: it sends the task back to Phase 4, which searches alternatives and asks the user if none fits.
"""

from app.schemas.monitor import EventOutcome, MonitorResult, Problem, Route


def route_monitor_result(result: MonitorResult) -> Route:
    if result.outcome is not EventOutcome.APPLIED:
        return Route.CONTINUE_MONITORING  # duplicate, stale or rejected events change nothing
    if result.completed:
        return Route.CLOSE
    match result.problem:
        case None:
            return Route.CONTINUE_MONITORING
        case Problem.DEADLINE_RISK:
            # Once money is held or captured, replanning must release or refund it (through the Guard) first.
            return Route.REPLAN_POST_PURCHASE if result.payment_committed else Route.ADAPT
        case Problem.NON_DELIVERY:
            return Route.REMEDY
        case Problem.WORK_REJECTED | Problem.PAYMENT_FAILED:
            return Route.ASK_USER
