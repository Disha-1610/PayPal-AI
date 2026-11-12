"""Run control over the compiled LangGraph: the only place API/worker code touches checkpointed state.

* ``load_task``      -> ``TaskView`` for the Monitor (from persisted state, never from an LLM)
* ``pending_approval`` -> the server-side terms an approval must match
* ``resume``         -> continue a run paused at ``interrupt()``
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from app.core.config import get_settings
from app.llm.deadline import llm_budget
from app.orchestrator.agents.negotiator.offer import parse_counter_offer
from app.schemas.authorization import PendingApproval
from app.schemas.monitor import TaskView
from app.schemas.state import TaskState


class UnknownRun(LookupError):
    """No checkpointed state exists for this run id."""


@dataclass(frozen=True)
class RunSnapshot:
    """A run's checkpointed state plus where it is paused: the nodes it will run next and any interrupt payloads."""

    values: TaskState
    next: tuple[str, ...]
    interrupts: tuple[object, ...]


class NotNegotiable(RuntimeError):
    """A counter-offer arrived while the run is busy elsewhere (paused for the user, or not yet through its first order)."""


class RunController:
    """``graph`` serves every run whose domain has no entry in ``by_domain``. Every graph must come from
    ``build_graph`` (same nodes and edges, different dependencies), so reads can use any of them; writes and
    invocations use the run's own domain graph, chosen from the ``domain`` held in its checkpointed state."""

    def __init__(self, graph: Any, *, by_domain: Mapping[str, Any] | None = None) -> None:
        self._graph = graph
        self._by_domain = dict(by_domain or {})

    @staticmethod
    def _config(run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": run_id}}

    def _for_domain(self, domain: str | None) -> Any:
        return self._by_domain.get(domain or "", self._graph)

    async def _graph_of(self, run_id: str) -> Any:
        if not self._by_domain:
            return self._graph
        snapshot = await self._graph.aget_state(self._config(run_id))
        return self._for_domain(snapshot.values.get("domain") if snapshot.values else None)

    async def _invoke(self, graph: Any, payload: Any, config: dict[str, Any]) -> TaskState:
        """One graph invocation with its LLM time budget; past it, agents fall back to their code-only path."""
        with llm_budget(get_settings().run_llm_budget_seconds):
            return await graph.ainvoke(payload, config)

    async def start(self, initial: TaskState) -> TaskState:
        """Run a new task until it finishes or pauses at an interrupt."""
        return await self._invoke(self._for_domain(initial.get("domain")), initial, self._config(initial["run_id"]))

    async def snapshot(self, run_id: str) -> RunSnapshot:
        snap = await self._graph.aget_state(self._config(run_id))
        if not snap.values:
            raise UnknownRun(run_id)
        interrupts = tuple(i.value for task in snap.tasks for i in task.interrupts)
        return RunSnapshot(values=snap.values, next=tuple(snap.next), interrupts=interrupts)

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
        graph = await self._graph_of(run_id)
        await graph.aupdate_state(self._config(run_id), {}, as_node="monitor_intake")
        return await self._invoke(graph, None, self._config(run_id))

    async def receive_counter_offer(self, run_id: str, raw: Mapping[str, object]) -> TaskState:
        """A counterparty's counter-offer. It is sanitized into structured fields first (``ValidationError`` rejects the
        whole offer, nothing is stored). If the run is waiting for the reply it resumes with it; otherwise a finished
        run re-enters at ``negotiation_intake``. Either way it reaches the Negotiator only through the Guard."""
        offer = parse_counter_offer(raw)
        snapshot = await self.snapshot(run_id)
        if "await_counterparty" in snapshot.next:
            return await self.resume(run_id, dict(raw))
        if snapshot.next or not snapshot.values.get("approvals"):
            raise NotNegotiable(run_id)
        graph = await self._graph_of(run_id)
        await graph.aupdate_state(self._config(run_id), {"incoming_offer": offer}, as_node="negotiation_intake")
        return await self._invoke(graph, None, self._config(run_id))

    async def cancel(self, run_id: str) -> TaskState:
        """End a paused run at the user's request: the graph finishes through ``stop_report`` with status ``cancelled``
        and any pending approval is withdrawn, so nothing can still be approved or resumed. Funds are released by the
        caller first (``MonitorWorker.release_for_cancel``); this only closes the run."""
        await (await self._graph_of(run_id)).aupdate_state(
            self._config(run_id),
            {"status": "cancelled", "stop_reason": "cancelled by the user", "pending_approval": None},
            as_node="stop_report",
        )
        return await self.state(run_id)

    async def fail(self, run_id: str, reason: str) -> TaskState:
        """Record that an invocation crashed: the run ends through ``stop_report`` with status ``failed``, so the
        checkpoint (not just a log line) says why and nothing still points at the node that raised. Use only where no
        money can be committed (the first invocation); later failures keep their checkpoint for the Monitor."""
        await (await self._graph_of(run_id)).aupdate_state(
            self._config(run_id),
            {"status": "failed", "stop_reason": reason, "errors": [reason], "pending_approval": None},
            as_node="stop_report",
        )
        return await self.state(run_id)

    async def retry_settlement(self, run_id: str) -> TaskState:
        """Re-run ``settle`` for a run that ended waiting for fulfilment evidence (status ``awaiting_confirmation``).
        Settlement is idempotent (same payment, same keys, permit re-verified): it captures on PASS evidence,
        voids on FAIL, and otherwise keeps waiting; it never creates a new order or approval."""
        graph = await self._graph_of(run_id)
        await graph.aupdate_state(self._config(run_id), {}, as_node="await_payer")
        return await self._invoke(graph, None, self._config(run_id))

    async def set_status(self, run_id: str, status: str) -> None:
        await (await self._graph_of(run_id)).aupdate_state(self._config(run_id), {"status": status})

    async def resume(self, run_id: str, value: object) -> TaskState:
        return await self._invoke(await self._graph_of(run_id), Command(resume=value), self._config(run_id))
