"""Composition root: real graph compiles with the real wiring, missing config degrades safely."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import bootstrap
from app.api.bootstrap import RuntimeConfigError, active_run_ids, background_dispatch, open_runtime
from app.core.config import Settings
from app.db.base import Base
from app.db.models.run import RunRecord


def complete(tmp_path, **override) -> Settings:
    values = dict(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'app.sqlite'}", checkpoint_sqlite_path=str(tmp_path / "cp.sqlite"),
        llm_api_key=SecretStr("llm"), channel3_api_key=SecretStr("c3"), approval_signing_key=SecretStr("k" * 32),
        paypal_client_id="id", paypal_client_secret=SecretStr("secret"), paypal_webhook_id="wh",
        paypal_payee_merchant_id="MERCHANT1",
    )
    return Settings(**{**values, **override})


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def app_with_writer() -> FastAPI:
    app = FastAPI()
    app.state.audit_writer = SimpleNamespace(enqueue=lambda event: None)
    return app


@pytest.mark.parametrize("missing", ["llm_api_key", "channel3_api_key", "approval_signing_key", "paypal_client_id",
                                     "paypal_client_secret", "paypal_webhook_id", "paypal_payee_merchant_id"])
async def test_each_missing_setting_is_named_and_nothing_is_installed(tmp_path, factory, missing):
    app = app_with_writer()
    with pytest.raises(RuntimeConfigError, match=missing.upper()):
        async with open_runtime(app, complete(tmp_path, **{missing: None}), factory=factory):
            pass
    assert not hasattr(app.state, "runs")


async def test_full_runtime_compiles_the_real_graph_and_installs_services(tmp_path, factory):
    app = app_with_writer()
    async with open_runtime(app, complete(tmp_path), factory=factory):
        assert app.state.runs is not None and app.state.run_service is not None
        assert app.state.paypal is not None and app.state.monitor_dispatch is not None
        assert app.state.run_service._platforms == {"channel3"}


async def test_active_runs_exclude_finished_ones(factory):
    async with factory() as session, session.begin():
        for i, status in enumerate(["running", "completed", "awaiting_approval", "failed"]):
            session.add(RunRecord(run_id=f"r{i}", user_id="u", idempotency_key=f"k{i}", goal="g", max_spend=1,
                                  currency="USD", status=status))
    assert sorted(await active_run_ids(factory)()) == ["r0", "r2"]


async def test_dispatch_returns_immediately_and_a_failing_handler_does_not_escape():
    seen, tasks = [], set()

    class Worker:
        async def handle(self, result):
            await asyncio.sleep(0)
            seen.append(result)
            raise RuntimeError("replan exploded")

    result = SimpleNamespace(event=SimpleNamespace(run_id="r1", event_id="e1"))
    await background_dispatch(Worker(), tasks)(result)
    assert seen == []  # not awaited inside the request
    await asyncio.gather(*tasks)  # the handler ran and its error was logged, not raised
    assert seen == [result] and not tasks
