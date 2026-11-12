"""Terminal node for every stop: a global limit, an exhausted Coordinator/Adapt, a Guard block or a rejection.

Reports once and ends the run. It never moves money: any committed funds are released by the Guard-checked
Payment Service paths (settle / Monitor), not here.
"""

from app.audit.events import AuditScope, Phase
from app.orchestrator.router.limits import GraphLimits, breached
from app.schemas.state import TaskState

STOP_WRITABLE = frozenset({"status", "stop_reason", "errors"})

_KEEP_STATUS = {"failed": "failed", "cancelled": "cancelled"}


def stop_report_node(state: TaskState, *, limits: GraphLimits, audit: AuditScope | None = None) -> dict[str, object]:
    cause = breached(state, limits)
    coordinator = state.get("coordinator")
    stop = coordinator.stop_reason if coordinator and coordinator.stop_reason else None
    errors = state.get("errors") or []
    if cause:
        reason = cause.value
    elif stop:
        reason = stop.value
    elif state.get("status") == "failed" and errors:
        reason = errors[-1]  # the failing node's own explanation beats the bare status
    else:
        reason = state.get("status", "unknown")
    status = _KEEP_STATUS.get(state.get("status", ""), "stopped")
    if audit:
        audit.emit(Phase.PLAN, "TASK_STOPPED", node="stop_report", decision=status, reason=reason)
    return {"status": status, "stop_reason": reason, "errors": [f"task stopped: {reason}"]}
