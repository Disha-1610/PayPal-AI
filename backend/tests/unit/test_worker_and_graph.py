"""Monitor worker (route execution, funds released before a replan) and the graph's approval gate."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.audit.events import AuditScope
from app.orchestrator.graph.approval import await_approval_node, request_approval_node
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.runtime import RunController, UnknownRun
from app.payments.service import PaymentService
from app.schemas.authorization import Action, PendingApproval
from app.schemas.monitor import EventOutcome, MonitorEvent, MonitorResult, Problem
from app.schemas.state import TaskState
from app.workers.monitor import MonitorWorker
from tests.unit.payment_fakes import FakeProvider
from tests.unit.test_payment_service import (  # noqa: F401  (factory is a fixture)
    ALL_MONEY,
    AUTH,
    FULL,
    FULL_APPROVALS,
    KEYS,
    TERMS,
    authorized_payment,
    factory,
    status,
)

NOW = datetime(2026, 11, 10, tzinfo=UTC)


class Sink:
    def __init__(self):
        self.actions = []

    def enqueue(self, event):
        self.actions.append((event.action, event.decision))


class FakeRuns:
    def __init__(self, payment_id=None):
        self.calls, self.payment_id = [], payment_id

    async def state(self, run_id):
        return {"authorization": AUTH, "approvals": list(FULL_APPROVALS.values()), "payment_id": str(self.payment_id)}

    async def replan(self, run_id):
        self.calls.append("replan")

    async def set_status(self, run_id, status):
        self.calls.append(status)


async def no_runs():
    return []


def result(problem=None, *, committed=False, completed=False, outcome=EventOutcome.APPLIED):
    event = MonitorEvent(
        event_id="e", run_id="r1", source="seller", domain="shopping", event_type="X", entity_id="o1", occurred_at=NOW
    )
    return MonitorResult(event=event, outcome=outcome, problem=problem, payment_committed=committed, completed=completed)


def make_worker(factory, runs, service=None):
    sink = Sink()
    return MonitorWorker(runs, service or PaymentService(FakeProvider()), factory, sink, KEYS, active_runs=no_runs), sink


async def test_deadline_risk_before_purchase_goes_straight_to_adapt(factory):
    runs = FakeRuns()
    worker, _ = make_worker(factory, runs)
    await worker.handle(result(Problem.DEADLINE_RISK))
    assert runs.calls == ["replan"]


async def test_unapplied_events_do_nothing_and_completion_closes(factory):
    runs = FakeRuns()
    worker, _ = make_worker(factory, runs)
    await worker.handle(result(Problem.DEADLINE_RISK, outcome=EventOutcome.DUPLICATE))
    await worker.handle(result(completed=True))
    assert runs.calls == ["completed"]


async def test_post_purchase_replan_voids_the_hold_before_adapt(factory):
    service, pid = await authorized_payment(factory, FakeProvider())
    runs = FakeRuns(pid)
    worker, _ = make_worker(factory, runs, service)
    await worker.handle(result(Problem.DEADLINE_RISK, committed=True))
    assert await status(factory, pid) == "voided" and runs.calls == ["replan"]


async def test_captured_money_blocks_replan_without_refund_permission(factory):
    service, pid = await authorized_payment(factory, FakeProvider())
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL)
    runs = FakeRuns(pid)
    worker, sink = make_worker(factory, runs, service)
    await worker.handle(result(Problem.DEADLINE_RISK, committed=True))
    assert await status(factory, pid) == "captured"  # never re-buys while money is committed
    assert runs.calls == ["release_blocked"]
    assert any(action == "REPLAN_BLOCKED" for action, _ in sink.actions)


# ---- graph -----------------------------------------------------------------------------------------------

PENDING = PendingApproval(action=Action.CREATE_ORDER, candidate_id="c1", platform="shop", terms=TERMS, terms_hash=TERMS.terms_hash())
CFG = {"configurable": {"thread_id": "r1"}}


def approval_graph():
    graph = StateGraph(TaskState)
    graph.add_node("request", request_approval_node)
    graph.add_node("wait", await_approval_node)
    graph.add_edge(START, "request")
    graph.add_edge("request", "wait")
    graph.add_edge("wait", END)
    return graph.compile(checkpointer=MemorySaver())


async def paused():
    graph = approval_graph()
    await graph.ainvoke({"run_id": "r1", "pending_approval": PENDING}, CFG)
    assert (await graph.aget_state(CFG)).next == ("wait",)
    return graph


async def test_approval_gate_pauses_then_accepts_matching_approvals():
    graph = await paused()
    resume = {"decision": "approve", "approvals": [a.model_dump(mode="json") for a in FULL_APPROVALS.values()]}
    done = await graph.ainvoke(Command(resume=resume), CFG)
    assert done["status"] == "approved" and len(done["approvals"]) == len(ALL_MONEY) and done["pending_approval"] is None


@pytest.mark.parametrize(
    "resume",
    [
        {"decision": "reject", "approvals": []},
        {"decision": "approve", "approvals": []},
        {"decision": "approve", "approvals": [{"nonsense": 1}]},
        "approve",
    ],
)
async def test_approval_gate_rejects_missing_or_malformed_approvals(resume):
    graph = await paused()
    done = await graph.ainvoke(Command(resume=resume), CFG)
    assert done["status"] == "approval_rejected" and not done.get("approvals")


async def test_approval_for_other_terms_is_rejected():
    graph = await paused()
    other = FULL_APPROVALS[Action.CREATE_ORDER].model_copy(update={"terms_hash": "f" * 64})
    done = await graph.ainvoke(Command(resume={"decision": "approve", "approvals": [other.model_dump(mode="json")]}), CFG)
    assert done["status"] == "approval_rejected" and not done.get("approvals")


def test_request_approval_audits_and_missing_terms_fail():
    sink = Sink()
    update = request_approval_node({"run_id": "r1", "pending_approval": PENDING}, audit=AuditScope(sink, "r1"))
    assert update == {"status": "awaiting_approval"} and sink.actions == [("APPROVAL_REQUESTED", "requested")]
    assert request_approval_node({"run_id": "r1"})["status"] == "failed"


def test_full_graph_compiles_with_every_node_wired():
    deps = GraphDeps(
        plugin=MagicMock(), registry=MagicMock(), text_source=MagicMock(), executor=MagicMock(),
        session_factory=MagicMock(), audit_sink=Sink(), terms_for=MagicMock(), approval_keys=KEYS,
    )
    nodes = set(build_graph(deps, MemorySaver()).get_graph().nodes)
    assert {"intent", "recheck", "await_approval", "place_order", "await_payer", "settle", "monitor_intake"} <= nodes


async def test_run_controller_loads_task_view_and_rejects_unknown_runs():
    class Graph:
        def __init__(self, values):
            self.values = values

        async def aget_state(self, config):
            return SimpleNamespace(values=self.values)

    view = await RunController(Graph({"domain": "shopping", "constraints": [], "anchor_at": NOW})).load_task("r1")
    assert (view.run_id, view.domain, view.anchor_at) == ("r1", "shopping", NOW)
    with pytest.raises(UnknownRun):
        await RunController(Graph({})).load_task("r1")
    with pytest.raises(UnknownRun):
        await RunController(Graph({"constraints": []})).load_task("r1")
