import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.events import Phase, audit_event, emit, emit_critical
from app.core.hashing import stable_hash
from app.db.base import Base
from app.db.models.audit import AuditLog


class Sink:
    def __init__(self) -> None:
        self.events = []

    def enqueue(self, event) -> None:
        self.events.append(event)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def test_audit_event_has_the_standard_shape_and_hashes_the_payload():
    event = audit_event("t1", Phase.DISCOVERY, "SEARCH_STARTED", node="router", payload={"q": "shoes"}, decision="allow", reason="ok")
    assert (event.run_id, event.phase, event.action, event.decision, event.reason) == ("t1", "discovery", "SEARCH_STARTED", "allow", "ok")
    assert event.input_hash == stable_hash({"q": "shoes"})
    assert "shoes" not in event.model_dump_json()  # raw input is never stored


def test_no_payload_means_no_hash_and_long_reasons_are_truncated():
    event = audit_event("t1", Phase.ADAPT, "X", node="n", reason="r" * 900)
    assert event.input_hash is None and len(event.reason) == 512


def test_emit_queues_on_the_sink():
    sink = Sink()
    emit(sink, "t1", Phase.INTENT, "INTENT_EXTRACTED", node="intent")
    assert [(e.phase, e.action) for e in sink.events] == [("intent", "INTENT_EXTRACTED")]


async def test_emit_critical_persists_inside_the_callers_transaction(factory):
    async with factory() as s, s.begin():
        await emit_critical(s, "t1", Phase.EXECUTION, "PAYMENT_CAPTURED", node="payment_service", decision="allow")
    async with factory() as s:
        row = (await s.execute(select(AuditLog))).scalar_one()
    assert (row.run_id, row.phase, row.action, row.decision) == ("t1", "execution", "PAYMENT_CAPTURED", "allow")


async def test_a_rolled_back_transaction_leaves_no_audit_row(factory):
    with pytest.raises(RuntimeError):
        async with factory() as s, s.begin():
            await emit_critical(s, "t1", Phase.EXECUTION, "PAYMENT_CAPTURED", node="payment_service")
            raise RuntimeError("action failed")
    async with factory() as s:
        assert (await s.execute(select(AuditLog))).first() is None
