"""Phase 2.2: a first-agent failure ends the run in the checkpoint, the record and the API alike; the resume guard holds."""

import asyncio
from decimal import Decimal

import pytest
from conftest import FakeProvider
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.run import RunRecord
from app.llm.providers import LLMError
from app.runs.service import NewRun, NotAwaiting, RunBusy, RunService
from app.runs.view import derive_status
from tests.unit.graph_harness import controller, initial


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_intent_llm_failure_ends_the_graph_failed_with_a_reason():
    runs = controller(FakeProvider(LLMError("Gemini API error 400")))
    final = await runs.start(initial())
    snap = await runs.snapshot("r1")
    assert final["status"] == "failed" and snap.next == ()
    assert "could not understand the request" in final["stop_reason"]
    assert derive_status(snap) == "failed"


async def test_intent_rejected_twice_fails_cleanly():
    bad = '{"goal": "shoes", "constraints": [{"field": "colour", "operator": "==", "value": "red", "strength": "hard", "evidence": "red"}]}'
    runs = controller(FakeProvider(bad, bad))
    final = await runs.start(initial())
    assert final["status"] == "failed" and (await runs.snapshot("r1")).next == ()


async def test_crash_in_first_invocation_is_recorded_in_checkpoint_record_and_view(factory, monkeypatch):
    from app.orchestrator.graph import builder

    async def node_bug(*_args, **_kwargs):  # not an LLM error: escapes the intent handler, as a code bug would
        raise RuntimeError("node bug")

    monkeypatch.setattr(builder, "extract_intent", node_bug)
    service = RunService(controller(FakeProvider()), factory, frozenset({"mock_amazon"}))
    record = await service.create("u1", "key-00000001", NewRun(goal="shoes", max_spend=Decimal("5000"), currency="INR"))
    await service.aclose()
    view = await service.view("u1", record.run_id)
    async with factory() as session:
        stored = await session.get(RunRecord, record.run_id)
    assert stored.status == "failed" and view.status == "failed" and "RuntimeError" in (view.stop_reason or "")
    assert record.run_id not in service._resuming


async def test_resume_guard_survives_status_polls_while_the_graph_runs(factory):
    release = asyncio.Event()

    async def slow():
        await release.wait()
        raise LLMError("bad key")

    runs = controller(FakeProvider(slow))
    service = RunService(runs, factory, frozenset({"mock_amazon"}))
    record = await service.create("u1", "key-00000002", NewRun(goal="shoes", max_spend=Decimal("5000"), currency="INR"))
    for _ in range(5):  # status polls while the intent call is still in flight
        await asyncio.sleep(0.01)
        assert (await service.view("u1", record.run_id)).status == "running"
    assert record.run_id in service._resuming  # a view() poll used to clear it
    with pytest.raises(RunBusy):
        await service.cancel("u1", record.run_id)
    with pytest.raises(NotAwaiting):
        await service.post_message("u1", record.run_id, "hi", "client-0001")
    release.set()
    await service.aclose()
    assert record.run_id not in service._resuming
    assert (await service.view("u1", record.run_id)).status == "failed"
