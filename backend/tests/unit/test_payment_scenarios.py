"""Mandatory payment scenarios: user rejects approval, worker crash mid-payment, user cancels during execution.

Real compiled graph, real payment service and ledger, in-memory PayPal fake; only the LLM and connectors are absent."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import func, select

from app.api.deps import Principal
from app.api.routes import runs as runs_routes
from app.db.models.payment import Payment, PaymentEvent
from app.db.models.run import RunRecord
from app.domains.shopping import SHOPPING
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.runtime import RunController
from app.payments.service import PaymentService
from app.runs.service import CannotCancel, RunBusy, RunService
from app.schemas.authorization import Action, PendingApproval
from app.schemas.recheck import MismatchAction, MismatchReport
from app.workers.monitor import MonitorWorker
from tests.unit.payment_fakes import FakeProvider
from tests.unit.test_payment_service import (  # noqa: F401  (factory is a fixture)
    AUTH,
    FULL,
    FULL_APPROVALS,
    KEYS,
    TERMS,
    authorized_payment,
    factory,
    make_payment,
    permit,
    status,
)

CONFIG = {"configurable": {"thread_id": "r1"}}
PENDING = PendingApproval(action=Action.CREATE_ORDER, candidate_id="shoe-1", platform="shop", terms=TERMS, terms_hash=TERMS.terms_hash())


class Crash(Exception):
    """Stands in for the worker process dying: the open transaction is rolled back, nothing after runs."""


def make_graph(factory, executor=None):
    deps = GraphDeps(
        plugin=SHOPPING, registry=MagicMock(), text_source=MagicMock(), executor=executor or MagicMock(), session_factory=factory,
        audit_sink=MagicMock(), terms_for=MagicMock(), approval_keys=KEYS,
    )
    graph = build_graph(deps, MemorySaver())
    return graph, RunController(graph)


async def count(factory, model):
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


# ---- user rejects the approval ---------------------------------------------------------------------------

async def test_rejecting_the_approval_stops_the_run_and_never_touches_payment(factory):
    executor = MagicMock(place_order=AsyncMock())
    graph, runs = make_graph(factory, executor)
    await graph.aupdate_state(
        CONFIG, {"run_id": "r1", "goal": "shoes", "domain": "shopping", "status": "rechecked", "authorization": AUTH,
                 "original_intent": "shoes", "pending_approval": PENDING,
                 "recheck_report": MismatchReport(action=MismatchAction.PROCEED)}, as_node="recheck",
    )
    await graph.ainvoke(None, CONFIG)
    assert (await graph.aget_state(CONFIG)).next == ("await_approval",)
    await runs.resume("r1", {"decision": "reject", "approvals": []})
    final = (await graph.aget_state(CONFIG)).values
    assert final["status"] == "stopped" and final["pending_approval"] is None and not final.get("approvals")
    executor.place_order.assert_not_called()
    assert await count(factory, Payment) == 0


# ---- worker crash mid-payment ----------------------------------------------------------------------------

async def test_crash_after_the_paypal_order_but_before_the_ledger_row_creates_one_order_on_retry(factory):
    provider = FakeProvider()
    approval = FULL_APPROVALS[Action.CREATE_ORDER]
    # The worker died right after PayPal accepted the order: PayPal has it, the ledger does not.
    await provider.create_order(
        request_id=f"{approval.approval_id}:create", amount=Decimal("5200"), currency="INR", payee_id="m1", reference="lost"
    )
    assert await count(factory, Payment) == 0
    async with factory() as s, s.begin():  # the restarted worker retries with the same approval
        created = await PaymentService(provider).create(
            s, permit=FULL, approval=approval, order_id="o1", amount=Decimal("5200"), payee_id="m1"
        )
    assert await count(factory, Payment) == 1 and created.payment.provider_order_id == "ord-1"
    assert len({key for name, key in provider.calls if name == "create"}) == 1  # same idempotency key: PayPal made one order


async def test_crash_after_the_hold_but_before_the_ledger_commit_authorizes_once_on_retry(factory):
    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    provider.order_status = "payer_approved"
    with pytest.raises(Crash):
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
            raise Crash
    assert await status(factory, pid) == "created"  # the ledger never saw the hold
    async with factory() as s, s.begin():
        await PaymentService(provider).authorize(s, pid, permit=FULL)
    assert await status(factory, pid) == "authorized"
    async with factory() as s:
        authorized = (await s.execute(select(func.count()).select_from(PaymentEvent).where(PaymentEvent.event_type == "authorized"))).scalar_one()
    assert authorized == 1 and len({k for n, k in provider.calls if n == "authorize"}) == 1


async def test_crash_after_the_capture_but_before_the_ledger_commit_captures_once_on_retry(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    with pytest.raises(Crash):
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL)
            raise Crash
    assert await status(factory, pid) == "authorized"
    async with factory() as s, s.begin():
        await PaymentService(provider).capture(s, pid, permit=FULL)
    assert await status(factory, pid) == "captured"
    assert len({k for n, k in provider.calls if n == "capture"}) == 1  # same key: PayPal captured once, not twice


# ---- user cancels during execution -----------------------------------------------------------------------

async def paused_run(factory, provider, *, stage):
    """A real graph paused at ``await_payer`` (it got there by running ``place_order``), with the payment in the
    ledger at ``stage``."""
    service, pid = await make_payment(factory, provider)
    if stage in {"authorized", "captured"}:
        provider.order_status = "payer_approved"
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
    if stage == "captured":
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL)
    placed = SimpleNamespace(created=SimpleNamespace(payment=SimpleNamespace(id=pid), approval_url="https://www.paypal.com/x"))
    graph, runs = make_graph(factory, MagicMock(place_order=AsyncMock(return_value=placed)))
    await graph.aupdate_state(
        CONFIG, {"run_id": "r1", "goal": "shoes", "domain": "shopping", "status": "guard_passed", "authorization": AUTH,
                 "original_intent": "shoes", "approvals": list(FULL_APPROVALS.values())}, as_node="guard_pay",
    )
    await graph.ainvoke(None, CONFIG)
    state = await graph.aget_state(CONFIG)
    assert state.next == ("await_payer",) and state.values["payment_id"] == str(pid)
    return graph, runs, service, pid


def run_service(factory, runs, payments):
    worker = MonitorWorker(runs, payments, factory, MagicMock(), KEYS, active_runs=lambda: [])
    return RunService(runs, factory, frozenset({"shop"}), release_funds=worker.release_for_cancel)


async def add_record(factory):
    async with factory() as s, s.begin():
        s.add(RunRecord(run_id="r1", user_id="u1", idempotency_key="k0000001", goal="shoes", max_spend=6000, currency="INR"))


async def test_cancelling_while_the_payer_has_not_approved_voids_the_order_and_ends_the_run(factory):
    provider = FakeProvider()
    graph, runs, payments, pid = await paused_run(factory, provider, stage="created")
    await add_record(factory)
    assert await run_service(factory, runs, payments).cancel("u1", "r1") is True
    assert await status(factory, pid) == "voided"
    end = await graph.aget_state(CONFIG)
    assert end.next == () and end.values["status"] == "cancelled" and end.values["stop_reason"] == "cancelled by the user"
    assert not any(name == "capture" for name, _ in provider.calls)


async def test_cancelling_with_a_hold_releases_the_funds_at_paypal_and_never_captures(factory):
    provider = FakeProvider()
    graph, runs, payments, pid = await paused_run(factory, provider, stage="authorized")
    await add_record(factory)
    await run_service(factory, runs, payments).cancel("u1", "r1")
    assert await status(factory, pid) == "voided"
    assert ("void", f"{pid}:void") in provider.calls and not any(name == "capture" for name, _ in provider.calls)
    async with factory() as s:
        assert (await s.get(RunRecord, "r1")).status == "stopped"


async def test_captured_money_cannot_be_cancelled_away_without_a_refund_grant(factory):
    provider = FakeProvider()
    graph, runs, payments, pid = await paused_run(factory, provider, stage="captured")
    await add_record(factory)
    with pytest.raises(CannotCancel):
        await run_service(factory, runs, payments).cancel("u1", "r1")
    assert await status(factory, pid) == "captured"  # nothing refunded without approval
    assert (await graph.aget_state(CONFIG)).next == ("await_payer",)  # and the run stays open


async def test_cancelling_before_any_order_needs_no_payment_call_and_withdraws_the_pending_approval(factory):
    provider = FakeProvider()
    graph, runs = make_graph(factory)
    await graph.aupdate_state(
        CONFIG, {"run_id": "r1", "goal": "shoes", "domain": "shopping", "status": "rechecked", "authorization": AUTH,
                 "original_intent": "shoes", "pending_approval": PENDING,
                 "recheck_report": MismatchReport(action=MismatchAction.PROCEED)}, as_node="recheck",
    )
    await graph.ainvoke(None, CONFIG)
    await add_record(factory)
    service = run_service(factory, runs, PaymentService(provider))
    assert await service.cancel("u1", "r1") is True
    end = await graph.aget_state(CONFIG)
    assert end.next == () and end.values["pending_approval"] is None and provider.calls == []


async def test_a_busy_or_finished_run_cannot_be_cancelled_and_other_users_see_404(factory):
    graph, runs, payments, _ = await paused_run(factory, FakeProvider(), stage="created")
    await add_record(factory)
    service = run_service(factory, runs, payments)
    assert await service.cancel("someone-else", "r1") is False
    service._resuming.add("r1")
    with pytest.raises(RunBusy):
        await service.cancel("u1", "r1")
    service._resuming.discard("r1")
    await service.cancel("u1", "r1")
    with pytest.raises(CannotCancel):  # already cancelled
        await service.cancel("u1", "r1")


async def test_cancel_route_status_codes(factory):
    graph, runs, payments, _ = await paused_run(factory, FakeProvider(), stage="created")
    await add_record(factory)
    app = FastAPI()
    app.include_router(runs_routes.router)
    app.state.run_service = run_service(factory, runs, payments)

    async def authenticate(request):
        return Principal(user_id=request.headers.get("x-user", "u1"), session_id="s")

    app.state.authenticator = authenticate
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    assert (await client.post("/runs/r1/cancel", headers={"x-user": "other"})).status_code == 404
    assert (await client.post("/runs/r1/cancel")).status_code == 202
    assert (await client.post("/runs/r1/cancel")).status_code == 409
    assert (await client.get("/runs/r1")).json()["status"] == "stopped"
