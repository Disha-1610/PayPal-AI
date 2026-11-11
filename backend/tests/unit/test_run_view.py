"""State -> client Run projection, plus the chat / payer-return / event-stream behaviour built on it."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import Principal
from app.api.routes import runs as runs_routes
from app.db.base import Base
from app.db.models.payment import Payment
from app.db.models.run import RunRecord
from app.orchestrator.graph.runtime import RunSnapshot, UnknownRun
from app.runs import service as service_module
from app.runs.service import RunService
from app.runs.view import client_status, derive_status, question_text, run_view
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, PendingApproval
from app.schemas.candidate import Candidate
from app.schemas.evaluation import RejectionKind
from app.schemas.evaluator import EvaluationReport, EvaluatorOutcome, RankedViable, RejectedCandidate

T0 = datetime(2026, 11, 11, tzinfo=UTC)
TERMS = ApprovalTerms(
    subject_id="sku-1", parties={"payee_id": "M1"}, currency="USD", max_total_minor=4999, expected_total_minor=4999,
    latest_acceptable_at=datetime(2026, 11, 20, tzinfo=UTC), display={"title": "Trail shoe", "seller": "channel3"},
)
PENDING = PendingApproval(
    action=Action.CREATE_ORDER, candidate_id="sku-1", platform="channel3", terms=TERMS, terms_hash=TERMS.terms_hash()
)
GOOD = Candidate(candidate_id="sku-1", source="channel3", domain="shopping", title="Trail shoe", total_cost=Decimal("49.99"),
                 currency="USD", rating=4.5, review_count=120, available_by=datetime(2026, 11, 15, tzinfo=UTC))
BAD = Candidate(candidate_id="sku-2", source="channel3", domain="shopping", title="Cheap shoe", total_cost=Decimal("9.99"),
                currency="USD")
REPORT = EvaluationReport(
    outcome=EvaluatorOutcome.VIABLE, reason="1 viable", attempt=1, evaluated_at=T0,
    ranked=(RankedViable(candidate_id="sku-1", score=87.34, code_score=80),),
    rejected=(RejectedCandidate(candidate_id="sku-2", kind=RejectionKind.FAILED, fields=("brand",), reason="brand must be Acme"),),
)
RECORD = RunRecord(run_id="r1", user_id="u1", idempotency_key="k", goal="shoes", max_spend=Decimal("100"), currency="USD",
                   status="running", created_at=T0, updated_at=T0)


def snap(values=None, next=(), interrupts=()):
    return RunSnapshot(values={"goal": "shoes", **(values or {})}, next=next, interrupts=interrupts)


# ---- status ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("next_nodes", "graph_status", "expected"),
    [
        (("await_approval",), "awaiting_approval", "awaiting_approval"),
        (("await_payer",), "awaiting_payer", "awaiting_payment"),
        (("ask_user",), "adapting", "awaiting_input"),
        ((), "settled", "completed"),
        ((), "stopped", "stopped"),
        ((), "cancelled", "stopped"),
        ((), "failed", "failed"),
        ((), "expired", "stopped"),
        ((), "awaiting_confirmation", "running"),
        (("discovery_search",), "searching", "running"),
    ],
)
def test_status_comes_from_where_the_graph_is_paused(next_nodes, graph_status, expected):
    assert derive_status(snap({"status": graph_status}, next_nodes)) == expected


def test_unknown_stored_status_means_running():
    assert client_status("release_blocked") == "running" and client_status("completed") == "completed"


# ---- projection ------------------------------------------------------------------------------------------

def test_pending_approval_exposes_exact_terms_only_while_awaiting_approval():
    waiting = run_view(RECORD, snap({"pending_approval": PENDING, "status": "awaiting_approval"}, ("await_approval",)))
    a = waiting.pending_approval
    assert (a.item, a.price_usd, a.seller, a.delivery_by, a.terms_hash) == ("Trail shoe", 49.99, "channel3", "2026-11-20", PENDING.terms_hash)
    assert run_view(RECORD, snap({"pending_approval": PENDING}, ("discovery_search",))).pending_approval is None


def test_candidates_hidden_until_evaluated_then_ranked_with_code_built_reasons():
    assert run_view(RECORD, snap({"candidates": [GOOD, BAD]})).candidates is None
    views = run_view(RECORD, snap({"candidates": [BAD, GOOD], "evaluation": REPORT})).candidates
    assert [(v.id, v.verdict, v.score) for v in views] == [("sku-1", "viable", 87.3), ("sku-2", "rejected", 0.0)]
    assert views[0].delivery_by == "2026-11-15" and views[1].rejection_reason == "brand must be Acme"


def test_payment_shows_the_approval_link_only_while_unauthorized():
    base = dict(id=None, run_id="r1", order_id="order-1", authorization_id="a", status="created", amount=Decimal("49.99"),
                currency="USD", idempotency_key="i", created_at=T0, updated_at=T0)
    created = run_view(RECORD, snap({"approval_url": "https://paypal.test/approve"}), payment=Payment(**base))
    assert created.payment.approval_url == "https://paypal.test/approve" and created.payment.amount == 49.99
    captured = run_view(RECORD, snap({"approval_url": "https://paypal.test/approve"}), payment=Payment(**{**base, "status": "captured"}))
    assert captured.payment.approval_url is None and captured.payment.status == "captured"


def test_steps_track_progress_and_mark_where_a_stopped_run_ended():
    running = run_view(RECORD, snap({"candidates": [GOOD]}))
    assert [s.status for s in running.steps] == ["done", "done", "running", "pending", "pending"]
    stopped = run_view(RECORD, snap({"candidates": [], "status": "stopped", "stop_reason": "no_progress"}))
    assert [s.status for s in stopped.steps] == ["done", "failed", "pending", "pending", "pending"]
    assert stopped.stop_reason == "no_progress" and stopped.status == "stopped"


def test_untrusted_free_text_has_no_path_into_the_view():
    view = run_view(RECORD, snap({"candidates": [GOOD], "evaluation": REPORT, "errors": ["secret internal detail"]}))
    assert "secret internal detail" not in view.model_dump_json()


def test_question_text_prefers_the_interrupt_message_then_intent_questions():
    asking = snap({"questions": ["What is your budget?"]}, ("ask_user",), ({"message": "Accept a higher budget?"},))
    assert question_text(asking) == "Accept a higher budget?"
    assert question_text(snap({"questions": ["What is your budget?"]}, ("ask_user",))) == "What is your budget?"
    assert question_text(snap({"questions": ["x"]}, ("discovery_search",))) is None


# ---- service + routes ------------------------------------------------------------------------------------

class Controller:
    def __init__(self):
        self.snapshot_value, self.resumed, self.fail_resume, self.gate = None, [], False, None

    async def snapshot(self, run_id):
        if self.snapshot_value is None:
            raise UnknownRun(run_id)
        return self.snapshot_value

    async def resume(self, run_id, value):
        if self.gate:
            await self.gate.wait()
        if self.fail_resume:
            raise RuntimeError("boom")
        self.resumed.append(value)


@pytest.fixture
async def env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s, s.begin():
        s.add(RunRecord(run_id="r1", user_id="u1", idempotency_key="k", goal="shoes", max_spend=100, currency="USD"))
    controller = Controller()
    service = RunService(controller, factory, frozenset({"channel3"}))
    app = FastAPI()
    app.include_router(runs_routes.router)
    app.state.run_service = service

    async def authenticate(request):
        return Principal(user_id=request.headers.get("x-user", "u1"), session_id="s")

    app.state.authenticator = authenticate
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    yield client, service, controller
    await engine.dispose()


def asking(controller):
    controller.snapshot_value = snap({"questions": ["What size?"], "status": "asking"}, ("ask_user",))


async def test_get_run_before_the_graph_checkpoints_returns_the_index_view(env):
    client, _, _ = env
    body = (await client.get("/runs/r1")).json()
    assert body["status"] == "running" and body["steps"] == [] and body["messages"] == []


async def test_a_question_is_stored_once_and_status_syncs_to_the_index(env):
    client, service, controller = env
    asking(controller)
    first = (await client.get("/runs/r1")).json()
    second = (await client.get("/runs/r1")).json()
    assert first["status"] == "awaiting_input"
    assert [m["text"] for m in second["messages"]] == ["What size?"] and second["messages"][0]["is_question"] is True
    assert (await service.get("u1", "r1")).status == "awaiting_input"


async def test_answer_resumes_with_the_text_and_retry_with_same_client_id_does_not_resume_twice(env):
    client, service, controller = env
    asking(controller)
    body = {"text": " size 9 ", "client_id": "client-0001"}
    first = await client.post("/runs/r1/messages", json=body)
    await service.aclose()
    again = await client.post("/runs/r1/messages", json=body)
    assert first.status_code == 201 and first.json()["text"] == "size 9" and again.json()["id"] == first.json()["id"]
    assert controller.resumed == ["size 9"]


async def test_message_when_not_asking_is_409_and_nothing_is_stored(env):
    client, _, controller = env
    controller.snapshot_value = snap({"status": "searching"}, ("discovery_search",))
    assert (await client.post("/runs/r1/messages", json={"text": "hi", "client_id": "client-0002"})).status_code == 409
    assert (await client.get("/runs/r1")).json()["messages"] == []


async def test_a_failed_resume_removes_the_message_so_the_user_can_retry(env):
    client, service, controller = env
    asking(controller)
    controller.fail_resume = True
    body = {"text": "size 9", "client_id": "client-0003"}
    await client.post("/runs/r1/messages", json=body)
    await service.aclose()
    controller.fail_resume = False
    retry = await client.post("/runs/r1/messages", json=body)
    await service.aclose()
    assert retry.status_code == 201 and controller.resumed == ["size 9"]


async def test_second_answer_while_one_is_resuming_is_rejected(env):
    client, service, controller = env
    asking(controller)
    controller.gate = asyncio.Event()
    assert (await client.post("/runs/r1/messages", json={"text": "a", "client_id": "client-0004"})).status_code == 201
    assert (await client.post("/runs/r1/messages", json={"text": "b", "client_id": "client-0005"})).status_code == 409
    controller.gate.set()
    await service.aclose()
    assert controller.resumed == ["a"]


@pytest.mark.parametrize("body", [{"text": "", "client_id": "client-0006"}, {"text": "x", "client_id": "short"},
                                  {"text": "x", "client_id": "bad id!bad id!"}, {"text": "x" * 2001, "client_id": "client-0007"}])
async def test_bad_message_bodies_are_422(env, body):
    assert (await env[0].post("/runs/r1/messages", json=body)).status_code == 422


async def test_payer_return_resumes_once_and_only_when_waiting_for_the_payer(env):
    client, service, controller = env
    controller.snapshot_value = snap({"status": "searching"}, ("discovery_search",))
    assert (await client.post("/runs/r1/payer-return")).status_code == 409
    controller.snapshot_value = snap({"status": "awaiting_payer"}, ("await_payer",))
    controller.gate = asyncio.Event()
    assert (await client.post("/runs/r1/payer-return")).status_code == 202
    assert (await client.post("/runs/r1/payer-return")).status_code == 202  # double click: still one resume
    controller.gate.set()
    await service.aclose()
    assert controller.resumed == [{"payer_returned": True}]


async def test_other_users_cannot_message_return_or_stream(env):
    client, _, controller = env
    asking(controller)
    other = {"x-user": "someone-else"}
    assert (await client.post("/runs/r1/messages", json={"text": "x", "client_id": "client-0008"}, headers=other)).status_code == 404
    assert (await client.post("/runs/r1/payer-return", headers=other)).status_code == 404
    assert (await client.get("/runs/r1/events", headers=other)).status_code == 404


async def test_event_stream_sends_a_snapshot_then_a_new_one_only_when_the_run_changes(env, monkeypatch):
    _, service, controller = env
    monkeypatch.setattr(service_module, "_EVENT_POLL_SECONDS", 0.01)
    asking(controller)
    stream = service.events("u1", "r1")
    first = await anext(stream)
    assert first.startswith("event: run.snapshot\ndata: ")
    assert json.loads(first.split("data: ", 1)[1])["run"]["status"] == "awaiting_input"
    controller.snapshot_value = snap({"status": "settled"}, ())
    second = await asyncio.wait_for(anext(stream), 2)  # unchanged polls emit nothing; this is the next change
    assert json.loads(second.split("data: ", 1)[1])["run"]["status"] == "completed"
    await stream.aclose()


async def test_event_stream_sends_keep_alives_when_idle(env, monkeypatch):
    _, service, controller = env
    monkeypatch.setattr(service_module, "_EVENT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(service_module, "_HEARTBEAT_SECONDS", 0.03)
    asking(controller)
    stream = service.events("u1", "r1")
    frames = [await asyncio.wait_for(anext(stream), 2) for _ in range(3)]
    # the first poll syncs the index status (updated_at moves once); after that the run is idle
    assert frames[0] and frames[-1] is None and sum(f is not None for f in frames) <= 2
    await stream.aclose()


async def test_controller_snapshot_reads_real_langgraph_pause_and_interrupt_payload():
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    from app.orchestrator.graph.runtime import RunController
    from app.schemas.state import TaskState

    def ask_user(state):
        interrupt({"type": "relaxation_request", "message": "Accept a higher budget?"})
        return {"status": "adapting"}

    graph = StateGraph(TaskState)
    graph.add_node("ask_user", ask_user)
    graph.add_edge(START, "ask_user")
    graph.add_edge("ask_user", END)
    controller = RunController(graph.compile(checkpointer=MemorySaver()))
    await controller.start({"run_id": "r9", "goal": "shoes", "status": "created"})
    snapshot = await controller.snapshot("r9")
    assert snapshot.next == ("ask_user",) and derive_status(snapshot) == "awaiting_input"
    assert question_text(snapshot) == "Accept a higher budget?"
    with pytest.raises(UnknownRun):
        await controller.snapshot("missing")
