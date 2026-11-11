"""Run control over the compiled LangGraph: the only place API/worker code touches checkpointed state.

* ``load_task``      -> ``TaskView`` for the Monitor (from persisted state, never from an LLM)
* ``pending_approval`` -> the server-side terms an approval must match
* ``resume``         -> continue a run paused at ``interrupt()``
"""

from typing import Any

from langgraph.types import Command

from app.schemas.authorization import PendingApproval
from app.schemas.monitor import TaskView
from app.schemas.state import TaskState


class UnknownRun(LookupError):
    """No checkpointed state exists for this run id."""


class RunController:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    async def start(self, initial: TaskState) -> TaskState:
        """Run a new task until it finishes or pauses at an interrupt."""
        return await self._graph.ainvoke(initial, self._config(initial["run_id"]))

    async def state(self, run_id: str) -> TaskState:
        snapshot = await self._graph.aget_state(self._config(run_id))
        if not snapshot.values:
            raise UnknownRun(run_id)
        return snapshot.values

    async def load_task(self, run_id: str) -> TaskView:
        state = await self.state(run_id)
        domain = state.get("domain")
        if not domain:
            raise UnknownRun(run_id)
        return TaskView(
            run_id=run_id, domain=domain, constraints=tuple(state.get("constraints", [])), anchor_at=state.get("anchor_at")
        )

    async def pending_approval(self, run_id: str) -> PendingApproval | None:
        return (await self.state(run_id)).get("pending_approval")

    async def replan(self, run_id: str) -> TaskState:
        """Re-enter the graph at ``monitor_intake`` (-> Adapt), abandoning whatever the run was paused on."""
        await self._graph.aupdate_state(self._config(run_id), {}, as_node="monitor_intake")
        return await self._graph.ainvoke(None, self._config(run_id))

    async def set_status(self, run_id: str, status: str) -> None:
        await self._graph.aupdate_state(self._config(run_id), {"status": status})

    async def resume(self, run_id: str, value: object) -> TaskState:
        return await self._graph.ainvoke(Command(resume=value), self._config(run_id))
