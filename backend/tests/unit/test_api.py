"""API handlers: approval issue + audit, and webhook status codes. Run controller and PayPal are fakes."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import Principal
from app.api.routes import approvals, webhooks
from app.core.config import settings
from app.db.base import Base
from app.db.models.audit import AuditLog
from app.monitor.webhooks.paypal import WebhookNotReady, WebhookRejected
from app.orchestrator.graph.runtime import RunSnapshot, UnknownRun
from app.runs.service import RunService
from app.orchestrator.guard.approval_guard import signed_approval_verifier
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, Approval, PendingApproval, TaskAuthorization
from app.schemas.monitor import EventOutcome, MonitorEvent, MonitorResult

KEY = "k" * 32
TERMS = ApprovalTerms(
    subject_id="sku-1", parties={"payee_id": "m1"}, currency="USD", max_total_minor=5_000, expected_total_minor=5_000,
    latest_acceptable_at=datetime(2026, 12, 1, tzinfo=UTC),
)
PENDING = PendingApproval(action=Action.CREATE_ORDER, candidate_id="c1", platform="mock", terms=TERMS, terms_hash=TERMS.terms_hash())
AUTH = TaskAuthorization(authorization_id="a1", run_id="r1", max_spend=Decimal(100), currency="USD")


class FakeRuns:
    def __init__(self, pending=PENDING):
        self.pending, self.resumed = pending, []

    async def state(self, run_id):
        if run_id != "r1":
            raise UnknownRun(run_id)
        return {"authorization": AUTH, "pending_approval": self.pending}

    async def resume(self, run_id, value):
        self.resumed.append(value)
        return {"status": "approved"}

    async def snapshot(self, run_id):
        await self.state(run_id)
        return RunSnapshot(values={}, next=("await_approval",) if self.pending else (), interrupts=())

    async def load_task(self, run_id):
        raise LookupError(run_id)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(settings, "approval_signing_key", SecretStr(KEY))


def make_app(factory, runs=None, *, auth=True, owner="u1") -> FastAPI:
    app = FastAPI()
    app.include_router(approvals.router)
    app.include_router(webhooks.router)
    app.state.session_factory = factory
    app.state.runs = runs or FakeRuns()
    # The real run service; only ownership lookup is faked: user u1 owns run r1.
    service = RunService(app.state.runs, factory, frozenset())
    async def owned(user_id, run_id):
        return object() if (user_id, run_id) == (owner, "r1") else None
    service.get = owned  # type: ignore[method-assign]
    app.state.run_service = service
    if auth:
        async def authenticate(request):
            return Principal(user_id="u1", session_id="s1")
        app.state.authenticator = authenticate
    return app


def post_approval(client, decision="approve", terms_hash=PENDING.terms_hash, run="r1"):
    return client.post(f"/runs/{run}/approval", json={"decision": decision, "terms_hash": terms_hash})


async def audit_rows(factory):
    async with factory() as session:
        return list((await session.execute(select(AuditLog))).scalars())


async def test_approve_issues_verifiable_approval_audits_and_resumes(factory):
    app = make_app(factory)
    with TestClient(app) as client:
        response = post_approval(client)
    assert response.status_code == 200 and response.json()["status"] == "approved"
    resume = app.state.runs.resumed[0]
    issued = [Approval.model_validate(a) for a in resume["approvals"]]
    assert {a.action for a in issued} == AUTH.approval_required_actions
    approval = issued[0]
    verify = signed_approval_verifier(
        current_terms=TERMS, keys={settings.approval_key_id: KEY.encode()}, run_id="r1", session_id="s1", approved_by="u1"
    )
    assert verify(approval)
    rows = await audit_rows(factory)
    assert [(r.action, r.decision, r.actor_id) for r in rows] == [("APPROVAL_RECEIVED", "approved", "u1")]


async def test_reject_audits_and_resumes_without_approval(factory):
    app = make_app(factory)
    with TestClient(app) as client:
        assert post_approval(client, "reject").status_code == 200
    assert app.state.runs.resumed == [{"decision": "reject", "approvals": []}]
    assert [r.decision for r in await audit_rows(factory)] == ["rejected"]


async def test_stale_terms_hash_is_refused_and_nothing_happens(factory):
    app = make_app(factory)
    with TestClient(app) as client:
        assert post_approval(client, terms_hash="0" * 64).status_code == 409
    assert app.state.runs.resumed == [] and await audit_rows(factory) == []


async def test_no_pending_unknown_run_and_unauthenticated(factory):
    with TestClient(make_app(factory, FakeRuns(pending=None))) as client:
        assert post_approval(client).status_code == 409
    with TestClient(make_app(factory)) as client:
        assert post_approval(client, run="nope").status_code == 404
    with TestClient(make_app(factory, auth=False)) as client:
        assert post_approval(client).status_code == 401


async def test_approval_for_another_users_run_is_not_found_and_changes_nothing(factory):
    app = make_app(factory, owner="someone-else")
    with TestClient(app) as client:
        assert post_approval(client).status_code == 404
    assert app.state.runs.resumed == [] and await audit_rows(factory) == []


async def test_decision_while_the_run_is_already_resuming_is_refused(factory):
    app = make_app(factory)
    app.state.run_service._resuming.add("r1")  # a first submit is still running the gate
    with TestClient(app) as client:
        assert post_approval(client).status_code == 409
    assert app.state.runs.resumed == [] and await audit_rows(factory) == []


async def test_missing_signing_key_fails_closed(factory, monkeypatch):
    monkeypatch.setattr(settings, "approval_signing_key", None)
    app = make_app(factory)
    with TestClient(app) as client:
        assert post_approval(client).status_code == 503
    assert app.state.runs.resumed == []


class FakePayPal:
    def __init__(self, error=None):
        self.error = error

    async def verify_webhook(self, **_):
        return True


def webhook_app(factory, monkeypatch, *, error=None, result=None):
    app = make_app(factory)
    app.state.paypal = FakePayPal()
    dispatched = []

    async def dispatch(res):
        dispatched.append(res)

    app.state.monitor_dispatch = dispatch

    async def fake_handle(*args, **kwargs):
        if error:
            raise error
        return result

    monkeypatch.setattr(webhooks, "handle_paypal_webhook", fake_handle)
    return app, dispatched


EVENT = MonitorEvent(
    event_id="e1", run_id="r1", source="paypal", domain="shopping", event_type="PAYMENT_CAPTURED", entity_id="payment:1",
    occurred_at=datetime(2026, 11, 10, tzinfo=UTC),
)


@pytest.mark.parametrize(
    ("error", "code"), [(WebhookRejected("bad"), 401), (WebhookNotReady("early"), 503)]
)
async def test_webhook_error_codes_drive_paypal_retry(factory, monkeypatch, error, code):
    app, dispatched = webhook_app(factory, monkeypatch, error=error)
    with TestClient(app) as client:
        assert client.post("/webhooks/paypal", json={"id": "x"}).status_code == code
    assert dispatched == []


async def test_webhook_applied_event_is_dispatched_and_others_are_not(factory, monkeypatch):
    applied = MonitorResult(event=EVENT, outcome=EventOutcome.APPLIED)
    app, dispatched = webhook_app(factory, monkeypatch, result=applied)
    with TestClient(app) as client:
        assert client.post("/webhooks/paypal", json={"id": "x"}).json() == {"status": "applied"}
    assert dispatched == [applied]
    app, dispatched = webhook_app(factory, monkeypatch, result=MonitorResult(event=EVENT, outcome=EventOutcome.DUPLICATE))
    with TestClient(app) as client:
        assert client.post("/webhooks/paypal", json={"id": "x"}).json() == {"status": "duplicate"}
    assert dispatched == []


async def test_webhook_rejects_non_json(factory, monkeypatch):
    app, _ = webhook_app(factory, monkeypatch)
    with TestClient(app) as client:
        assert client.post("/webhooks/paypal", content=b"nope").status_code == 400
