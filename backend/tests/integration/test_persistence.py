import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.audit.service import AuditEvent, AuditWriter, hash_input, record_critical
from app.core.config import Settings
from app.db.base import Base
from app.db.models import AuditLog, Payment, PaymentEvent
from app.db.models.payment import PaymentStatus, can_transition
from app.orchestrator.graph.checkpointer import open_checkpointer
from app.schemas.authorization import TaskAuthorization
from app.schemas.state import TaskState, make_initial_state

BACKEND = Path(__file__).resolve().parents[2]


def _alembic_cfg(url: str) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_migrations_apply_and_match_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    cfg = _alembic_cfg(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        assert conn.execute(text("select version_num from alembic_version")).scalar() == "0008"
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []  # migration and models are in sync
    command.downgrade(cfg, "base")


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _event(**kw) -> AuditEvent:
    return AuditEvent(run_id="r1", node="guard", actor_type="system", action="TASK_EXECUTED",
                      input_hash=hash_input({"a": 1}), decision="allow", **kw)


async def test_critical_audit_commits_and_rolls_back_with_action(factory):
    async with factory() as s:
        async with s.begin():
            await record_critical(s, _event())
    with pytest.raises(RuntimeError):
        async with factory() as s:
            async with s.begin():
                await record_critical(s, _event(reason="rolled back"))
                raise RuntimeError("action failed after audit")
    async with factory() as s:
        rows = (await s.execute(text("select reason from audit_logs"))).all()
    assert len(rows) == 1


async def test_audit_writer_persists_async(factory):
    writer = AuditWriter(factory)
    writer.start()
    writer.enqueue(_event())
    await writer.stop()
    async with factory() as s:
        assert (await s.execute(text("select count(*) from audit_logs"))).scalar() == 1


def test_hash_input_is_stable():
    assert hash_input({"b": 1, "a": 2}) == hash_input({"a": 2, "b": 1})


async def test_payment_idempotency_key_unique(factory):
    def pay(order):
        return Payment(run_id="r1", order_id=order, authorization_id="a", amount=Decimal("4500.00"),
                       currency="INR", idempotency_key="key-1")

    async with factory() as s:
        s.add(pay("o1"))
        await s.commit()
        s.add(pay("o2"))
        with pytest.raises(IntegrityError):
            await s.commit()


def test_payment_transitions():
    assert can_transition(PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED)
    assert not can_transition(PaymentStatus.CAPTURED, PaymentStatus.VOIDED)
    assert not can_transition(PaymentStatus.CREATED, PaymentStatus.CAPTURED)


async def test_sqlite_checkpointer_persists_state(tmp_path):
    cfg = Settings(database_url="sqlite:///x.sqlite", checkpoint_sqlite_path=str(tmp_path / "cp.sqlite"))
    auth = TaskAuthorization(run_id="r1", max_spend=Decimal(5000))

    def node(state: TaskState) -> dict:
        return {"status": "done"}

    async with open_checkpointer(cfg) as saver:
        graph = StateGraph(TaskState)
        graph.add_node("n", node)
        graph.add_edge(START, "n")
        graph.add_edge("n", END)
        app = graph.compile(checkpointer=saver)
        thread = {"configurable": {"thread_id": "r1"}}
        await app.ainvoke(make_initial_state(run_id="r1", original_intent="buy", authorization=auth), thread)
        restored = (await app.aget_state(thread)).values
    assert restored["status"] == "done"
    assert restored["authorization"] == auth
