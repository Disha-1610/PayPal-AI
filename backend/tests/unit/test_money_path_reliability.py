"""Phase 2.4: PayPal errors on the money path never crash a run or a worker; delivery outcomes resume settlement."""

from uuid import uuid4

from app.payments.provider import ProviderError
from app.schemas.monitor import EventOutcome, MonitorEvent, MonitorResult, Problem
from tests.unit.payment_fakes import FakeProvider
from tests.unit.test_payment_service import authorized_payment, factory, status  # noqa: F401  (factory is a fixture)
from tests.unit.test_worker_and_graph import NOW, FakeRuns, make_worker, result


class SettlingRuns(FakeRuns):
    """A run whose settlement ended waiting for delivery evidence."""

    def __init__(self, payment_id=None, status="awaiting_confirmation"):
        super().__init__(payment_id or uuid4())
        self.status = status

    async def state(self, run_id):
        return {**await super().state(run_id), "status": self.status}

    async def retry_settlement(self, run_id):
        self.calls.append("retry_settlement")


def event_result(event_type: str, problem: Problem | None = None) -> MonitorResult:
    event = MonitorEvent(
        event_id=f"e-{event_type}", run_id="r1", source="seller", domain="shopping", event_type=event_type, entity_id="o1",
        occurred_at=NOW,
    )
    return MonitorResult(event=event, outcome=EventOutcome.APPLIED, problem=problem)


async def test_delivered_event_resumes_settlement_of_a_held_payment(factory):
    runs = SettlingRuns()
    worker, _ = make_worker(factory, runs)
    await worker.handle(event_result("DELIVERED"))
    assert runs.calls == ["retry_settlement"]  # used to do nothing: the hold was never captured


async def test_delivery_failure_on_a_held_payment_settles_instead_of_refunding(factory):
    runs = SettlingRuns()
    worker, _ = make_worker(factory, runs)
    await worker.handle(event_result("DELIVERY_FAILED", Problem.NON_DELIVERY))
    assert runs.calls == ["retry_settlement"]  # settle voids on FAIL evidence; a refund of a hold is refused


async def test_progress_events_do_not_burn_verification_attempts(factory):
    runs = SettlingRuns()
    worker, _ = make_worker(factory, runs)
    await worker.handle(event_result("SHIPPED"))
    assert runs.calls == []


async def test_finished_runs_are_not_resettled(factory):
    runs = SettlingRuns(status="settled")
    worker, _ = make_worker(factory, runs)
    await worker.handle(event_result("DELIVERED"))
    assert runs.calls == []


async def test_paypal_unreachable_during_release_blocks_the_replan_without_crashing(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.fail_next = ProviderError("PayPal POST void failed: ConnectTimeout", uncertain=True)
    provider.get_authorization_fails = True  # the follow-up status read fails too: outcome unknown
    runs = FakeRuns(pid)
    worker, sink = make_worker(factory, runs, service)
    await worker.handle(result(Problem.DEADLINE_RISK, committed=True))  # used to raise ProviderError out of handle()
    assert runs.calls == ["release_blocked"]
    assert await status(factory, pid) == "authorized"  # nothing recorded as voided while PayPal's answer is unknown
    assert ("REPLAN_BLOCKED", "release_failed") in sink.actions


# ---- the real compiled graph ---------------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from app.orchestrator.executor.state import ExecutionState  # noqa: E402
from tests.unit.test_payment_scenarios import CONFIG, make_graph  # noqa: E402
from tests.unit.test_payment_service import AUTH, FULL_APPROVALS  # noqa: E402


async def _at_settle(graph):
    """A run whose payer just returned from PayPal: the next node is ``settle``."""
    await graph.aupdate_state(
        CONFIG,
        {"run_id": "r1", "domain": "shopping", "original_intent": "shoes", "authorization": AUTH, "status": "awaiting_payer",
         "approvals": list(FULL_APPROVALS.values()), "payment_id": str(uuid4()), "order_id": "o1", "settle_attempt": 0},
        as_node="await_payer",
    )


async def test_paypal_timeout_during_settle_pauses_for_a_retry_instead_of_crashing(factory):
    executor = MagicMock(settle=AsyncMock(side_effect=ProviderError("PayPal POST capture failed: ReadTimeout", uncertain=True)))
    graph, _ = make_graph(factory, executor)
    await _at_settle(graph)
    await graph.ainvoke(None, CONFIG)  # used to raise ProviderError out of the graph with the hold in place
    snap = await graph.aget_state(CONFIG)
    assert snap.next == ("await_payer",) and snap.values["status"] == "payer_pending"


async def test_definite_paypal_refusal_during_settle_goes_to_adapt(factory):
    executor = MagicMock(settle=AsyncMock(side_effect=ProviderError("refused with 422")))
    graph, _ = make_graph(factory, executor)
    await _at_settle(graph)
    try:
        await graph.ainvoke(None, CONFIG)
    except Exception:
        pass  # Adapt and the search beyond it use mocked connectors
    history = [s.values.get("status") async for s in graph.aget_state_history(CONFIG)]
    assert "payment_refused" in history


async def test_settlement_waiting_for_evidence_is_captured_when_the_monitor_retries_it(factory):
    executor = MagicMock(settle=AsyncMock(side_effect=[
        SimpleNamespace(state=ExecutionState.AWAITING_CONFIRMATION), SimpleNamespace(state=ExecutionState.SETTLED),
    ]))
    graph, runs = make_graph(factory, executor)
    await _at_settle(graph)
    await graph.ainvoke(None, CONFIG)
    assert (await runs.state("r1"))["status"] == "awaiting_confirmation"
    final = await runs.retry_settlement("r1")  # what MonitorWorker does on a DELIVERED event
    assert final["status"] == "settled" and final["settle_attempt"] == 2
    assert [c.kwargs["attempt"] for c in executor.settle.await_args_list] == [1, 2]
