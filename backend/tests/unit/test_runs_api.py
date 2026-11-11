"""Run routes and service: idempotent create, ownership scoping, pagination, background start."""

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import Principal
from app.api.routes import runs as runs_routes
from app.db.base import Base
from app.runs.service import RunService, intent_text, NewRun
from app.schemas.authorization import Action
from decimal import Decimal


class FakeController:
    def __init__(self, *, fail=False):
        self.started, self.fail = [], fail

    async def start(self, initial):
        if self.fail:
            raise RuntimeError("graph exploded")
        self.started.append(initial)
        return {"status": "awaiting_approval"}


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def make_client(factory, controller=None, user="u1"):
    controller = controller or FakeController()
    service = RunService(controller, factory, frozenset({"channel3"}))
    app = FastAPI()
    app.include_router(runs_routes.router)
    app.state.run_service = service

    async def authenticate(request):
        return Principal(user_id=request.headers.get("x-user", user), session_id="s1")

    app.state.authenticator = authenticate
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, service, controller


BODY = {"goal": "running shoes size 9", "max_spend": 5000, "currency": "INR"}


def headers(key="key-0000001", user=None):
    h = {"Idempotency-Key": key}
    if user:
        h["x-user"] = user
    return h


async def test_create_starts_the_run_in_the_background_with_a_scoped_authorization(factory):
    client, service, controller = make_client(factory)
    response = await client.post("/runs", json=BODY, headers=headers())
    assert response.status_code == 201
    await service.aclose()
    (initial,) = controller.started
    assert initial["run_id"] == response.json()["run_id"]
    auth = initial["authorization"]
    assert auth.permitted_platforms == {"channel3"} and auth.max_spend == 5000 and auth.currency == "INR"
    assert Action.EXECUTE_REFUND not in auth.permitted_actions and Action.NEGOTIATE not in auth.permitted_actions
    assert "Budget: 5000" in initial["original_intent"]
    fetched = (await client.get(f"/runs/{response.json()['run_id']}", headers=headers())).json()
    assert fetched["status"] == "awaiting_approval" and fetched["goal"] == BODY["goal"] and fetched["steps"] == []


async def test_same_idempotency_key_returns_the_same_run_without_starting_twice(factory):
    client, service, controller = make_client(factory)
    first = await client.post("/runs", json=BODY, headers=headers())
    second = await client.post("/runs", json=BODY, headers=headers())
    await service.aclose()
    assert first.json() == second.json() and len(controller.started) == 1


async def test_a_graph_failure_marks_the_run_failed_instead_of_losing_it(factory):
    client, service, _ = make_client(factory, FakeController(fail=True))
    run_id = (await client.post("/runs", json=BODY, headers=headers())).json()["run_id"]
    await service.aclose()
    assert (await client.get(f"/runs/{run_id}")).json()["status"] == "failed"


async def test_other_users_run_is_a_404(factory):
    client, service, _ = make_client(factory)
    run_id = (await client.post("/runs", json=BODY, headers=headers())).json()["run_id"]
    await service.aclose()
    assert (await client.get(f"/runs/{run_id}", headers={"x-user": "someone-else"})).status_code == 404
    assert (await client.get("/runs", headers={"x-user": "someone-else"})).json()["items"] == []


@pytest.mark.parametrize(
    "body",
    [{**BODY, "max_spend": 0}, {**BODY, "currency": "EUR"}, {**BODY, "goal": "x"}, {**BODY, "extra": 1}],
)
async def test_invalid_bodies_are_rejected(factory, body):
    client, _, controller = make_client(factory)
    assert (await client.post("/runs", json=body, headers=headers())).status_code == 422
    assert controller.started == []


async def test_idempotency_key_is_required(factory):
    client, _, _ = make_client(factory)
    assert (await client.post("/runs", json=BODY)).status_code == 422


async def test_list_is_newest_first_and_paginates_by_cursor(factory):
    client, service, _ = make_client(factory)
    ids = [(await client.post("/runs", json=BODY, headers=headers(f"key-{i:07d}"))).json()["run_id"] for i in range(3)]
    await service.aclose()
    first = (await client.get("/runs", params={"limit": 2})).json()
    assert [r["id"] for r in first["items"]] == ids[::-1][:2] and first["next_cursor"]
    second = (await client.get("/runs", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert [r["id"] for r in second["items"]] == ids[::-1][2:] and second["next_cursor"] is None


async def test_bad_cursor_is_400(factory):
    client, _, _ = make_client(factory)
    assert (await client.get("/runs", params={"cursor": "not-a-cursor"})).status_code == 400


async def test_unauthenticated_requests_fail_closed(factory):
    _, service, _ = make_client(factory)
    app = FastAPI()
    app.include_router(runs_routes.router)
    app.state.run_service = service
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    assert (await client.get("/runs")).status_code == 401


def test_intent_text_carries_budget_and_deadline():
    from datetime import UTC, datetime

    text = intent_text(NewRun("blue kettle", Decimal("40.50"), "USD", datetime(2026, 12, 1, tzinfo=UTC)))
    assert text == "blue kettle Budget: 40.50 USD. Must be delivered by 2026-12-01."
