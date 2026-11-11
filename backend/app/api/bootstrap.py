"""Composition root: builds the real graph, services and background workers and installs them on ``app.state``.

Everything is wired here and nowhere else. Missing configuration raises ``RuntimeConfigError`` naming what is
absent; the caller decides whether to run degraded (routes that need the runtime answer 503, never guess).
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import MonitorDispatch
from app.audit.service import AuditWriter
from app.connectors.channel3.connector import Channel3Connector
from app.connectors.registry import ConnectorRegistry
from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.run import RunRecord
from app.db.repositories.connector_text import SqlTextSource, SqlTextStore
from app.db.session import SessionFactory
from app.domains.shopping import SHOPPING
from app.domains.shopping_execution import ConnectorTermsObserver, MonitorEventEvidence, ShoppingTerms
from app.llm.providers import LLMError, build_provider
from app.monitor.events.processor import EventProcessor
from app.orchestrator.executor.executor import Executor
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.checkpointer import open_checkpointer
from app.orchestrator.graph.runtime import RunController
from app.orchestrator.guard.approval_guard import ApprovalError, approval_keys
from app.payments.paypal.client import PayPalClient
from app.payments.provider import ProviderError
from app.payments.reconciliation import Reconciler
from app.payments.service import PaymentService
from app.runs.service import RunService
from app.schemas.monitor import MonitorResult
from app.workers.monitor import MonitorWorker
from app.workers.reconciler import run_reconciler
from app.workers.sweeper import run_sweeper

logger = get_logger(__name__)

SWEEP_INTERVAL_SECONDS = 300.0
RECONCILE_INTERVAL_SECONDS = 600.0
_TERMINAL_STATUSES = ("completed", "failed", "stopped", "cancelled")


class RuntimeConfigError(RuntimeError):
    """Required configuration is missing or invalid."""


def _require(settings: Settings) -> None:
    missing = [
        name
        for name, value in {
            "LLM_API_KEY": settings.llm_api_key,
            "CHANNEL3_API_KEY": settings.channel3_api_key,
            "APPROVAL_SIGNING_KEY": settings.approval_signing_key,
            "PAYPAL_CLIENT_ID": settings.paypal_client_id,
            "PAYPAL_CLIENT_SECRET": settings.paypal_client_secret,
            "PAYPAL_WEBHOOK_ID": settings.paypal_webhook_id,
            "PAYPAL_PAYEE_MERCHANT_ID": settings.paypal_payee_merchant_id,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeConfigError(f"missing configuration: {', '.join(missing)}")


def active_run_ids(factory: async_sessionmaker[AsyncSession]):
    """Runs the deadline sweep should look at: everything not finished."""

    async def load() -> Sequence[str]:
        async with factory() as session:
            rows = await session.execute(select(RunRecord.run_id).where(RunRecord.status.not_in(_TERMINAL_STATUSES)))
            return list(rows.scalars())

    return load


def background_dispatch(worker: MonitorWorker, tasks: set[asyncio.Task[object]]) -> MonitorDispatch:
    """Handle an applied Monitor result off the webhook request: a replan can take minutes and PayPal expects a
    fast 2xx. The event is already stored, so a failed handler is logged and picked up again by the sweep."""

    async def handle(result: MonitorResult) -> None:
        try:
            await worker.handle(result)
        except Exception:
            logger.exception("monitor handler failed", extra={"run_id": result.event.run_id})

    async def dispatch(result: MonitorResult) -> None:
        task = asyncio.create_task(handle(result), name=f"monitor-{result.event.event_id}")
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    return dispatch


@asynccontextmanager
async def open_runtime(
    app: FastAPI, settings: Settings, *, factory: async_sessionmaker[AsyncSession] = SessionFactory
) -> AsyncIterator[None]:
    _require(settings)
    assert settings.channel3_api_key and settings.paypal_payee_merchant_id  # narrowed by _require
    try:
        keys = approval_keys(settings)
        provider = build_provider(settings)
        paypal = PayPalClient(settings)
    except (ApprovalError, LLMError, ProviderError) as exc:
        raise RuntimeConfigError(str(exc)) from exc

    writer: AuditWriter = app.state.audit_writer
    registry = ConnectorRegistry(SqlTextStore(factory))
    registry.register(
        Channel3Connector(
            settings.channel3_api_key.get_secret_value(), base_url=settings.channel3_base_url,
            country=settings.channel3_country, currency=settings.channel3_currency, timeout=settings.channel3_timeout_seconds,
        )
    )
    payments = PaymentService(paypal)
    observer = ConnectorTermsObserver(registry)
    executor = Executor(payments, observer, MonitorEventEvidence(observer, factory))
    deps = GraphDeps(
        plugin=SHOPPING, registry=registry, text_source=SqlTextSource(factory), executor=executor, session_factory=factory,
        audit_sink=writer, terms_for=ShoppingTerms(settings.paypal_payee_merchant_id), approval_keys=keys, provider=provider,
    )
    stop = asyncio.Event()
    workers: list[asyncio.Task[None]] = []
    background: set[asyncio.Task[object]] = set()
    run_service: RunService | None = None
    try:
        async with open_checkpointer(settings) as checkpointer:
            runs = RunController(build_graph(deps, checkpointer))
            worker = MonitorWorker(runs, payments, factory, writer, keys, active_runs=active_run_ids(factory))
            run_service = RunService(runs, factory, registry.connector_ids, release_funds=worker.release_for_cancel)
            app.state.paypal = paypal
            app.state.session_factory = factory
            app.state.runs = runs
            app.state.run_service = run_service
            app.state.monitor_dispatch = background_dispatch(worker, background)
            workers.append(
                asyncio.create_task(
                    run_sweeper(worker, EventProcessor(runs.load_task), interval_seconds=SWEEP_INTERVAL_SECONDS, stop=stop),
                    name="deadline-sweeper",
                )
            )
            workers.append(
                asyncio.create_task(
                    run_reconciler(Reconciler(paypal, payments), factory, interval_seconds=RECONCILE_INTERVAL_SECONDS, stop=stop),
                    name="payment-reconciler",
                )
            )
            logger.info("runtime ready", extra={"platforms": sorted(registry.connector_ids)})
            yield
    finally:
        stop.set()
        for task in workers:
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.gather(*background, return_exceptions=True)
        if run_service is not None:
            await run_service.aclose()
        await paypal.aclose()
