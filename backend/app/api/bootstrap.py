"""Composition root: builds the real graph, services and background workers and installs them on ``app.state``.

Everything is wired here and nowhere else. Missing configuration raises ``RuntimeConfigError`` naming what is
absent; the caller decides whether to run degraded (routes that need the runtime answer 503, never guess).
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import timedelta

from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import MonitorDispatch
from app.audit.service import AuditWriter
from app.connectors.channel3.connector import Channel3Connector
from app.connectors.registry import ConnectorRegistry
from app.core.config import Settings
from app.core.logging import get_logger
from app.counterparty.desk import CounterpartyDesk
from app.counterparty.engagement import (
    QuoteMilestonePlanner,
    QuoteTermsObserver,
    RoutedCounterparty,
)
from app.counterparty.tokens import ReplyTokens
from app.counterparty.transports import HttpAgentClient, SmtpEmail, TwilioText
from app.db.models.run import RunRecord
from app.db.repositories.connector_text import SqlTextSource, SqlTextStore
from app.db.session import SessionFactory
from app.domains.a2a import A2A
from app.domains.contractor import CONTRACTOR
from app.domains.contractor.milestone import MilestoneService
from app.domains.engagement import EngagementTerms, PlanTerms, engagement_envelope
from app.domains.shopping import SHOPPING
from app.domains.shopping_execution import (
    ConnectorTermsObserver,
    MonitorEventEvidence,
    ShoppingTerms,
    TermsObserver,
)
from app.llm.providers import LLMError, build_light_provider, build_provider
from app.monitor.events.processor import EventProcessor
from app.orchestrator.executor.executor import Executor
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.checkpointer import open_checkpointer
from app.orchestrator.graph.milestones import MilestoneFlow, MilestonePlanner
from app.orchestrator.graph.negotiation import CounterpartyChannel
from app.orchestrator.graph.runtime import RunController
from app.orchestrator.guard.approval_guard import ApprovalError, approval_keys
from app.payments.paypal.client import PayPalClient
from app.payments.provider import ProviderError
from app.payments.reconciliation import Reconciler
from app.payments.service import PaymentService
from app.runs.service import RunService
from app.sandbox.catalog import AGENT_SOURCE, CONTRACTOR_SOURCE, OFFERINGS
from app.sandbox.counterparty import (
    QuotedTermsObserver,
    SandboxConnector,
    SandboxCounterparty,
    SandboxMilestoneObserver,
    SandboxMilestonePlanner,
)
from app.schemas.counterparty import USER_SUPPLIED_SOURCE
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


def build_desk(settings: Settings, factory: async_sessionmaker[AsyncSession]) -> CounterpartyDesk | None:
    """Outreach to counterparties the user names. Off (None) without a signing key; each channel is on only when
    its own settings are present, so a deployment can offer just email, or just agents."""
    if settings.counterparty_signing_key is None:
        return None
    try:
        tokens = ReplyTokens(settings.counterparty_signing_key.get_secret_value())
    except ValueError as exc:
        raise RuntimeConfigError(str(exc)) from exc
    email = None
    if settings.smtp_host and settings.smtp_from:
        email = SmtpEmail(
            host=settings.smtp_host, port=settings.smtp_port, sender=settings.smtp_from, username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            starttls=settings.smtp_starttls,
        )
    text = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        text = TwilioText(
            settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value(),
            sms_from=settings.twilio_sms_from, whatsapp_from=settings.twilio_whatsapp_from,
        )
    return CounterpartyDesk(
        factory, tokens, public_base_url=settings.public_api_base_url, email=email, text=text,
        agents=HttpAgentClient(timeout=settings.a2a_timeout_seconds),
        reply_window=timedelta(hours=settings.counterparty_reply_hours), grace=timedelta(hours=settings.counterparty_grace_hours),
    )


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


async def _drain(
    stop: asyncio.Event, workers: list[asyncio.Task[None]], background: set[asyncio.Task[object]], run_service: RunService | None
) -> None:
    """Stop the periodic workers and wait for in-flight graph work. Safe to call twice."""
    stop.set()
    for task in workers:
        with suppress(asyncio.CancelledError):
            await task
    await asyncio.gather(*background, return_exceptions=True)
    if run_service is not None:
        await run_service.aclose()


@asynccontextmanager
async def open_runtime(
    app: FastAPI, settings: Settings, *, factory: async_sessionmaker[AsyncSession] = SessionFactory
) -> AsyncIterator[None]:
    _require(settings)
    assert settings.channel3_api_key and settings.paypal_payee_merchant_id  # narrowed by _require
    try:
        keys = approval_keys(settings)
        provider = build_provider(settings)
        light_provider = build_light_provider(settings)
        paypal = PayPalClient(settings)
    except (ApprovalError, LLMError, ProviderError) as exc:
        raise RuntimeConfigError(str(exc)) from exc

    writer: AuditWriter = app.state.audit_writer
    registry = ConnectorRegistry(SqlTextStore(factory))
    channel3 = Channel3Connector(
        settings.channel3_api_key.get_secret_value(), base_url=settings.channel3_base_url,
        country=settings.channel3_country, currency=settings.channel3_currency, timeout=settings.channel3_timeout_seconds,
    )
    registry.register(channel3)
    payments = PaymentService(paypal)
    observer = ConnectorTermsObserver(registry)
    executor = Executor(payments, observer, MonitorEventEvidence(observer, factory))
    deps = GraphDeps(
        plugin=SHOPPING, registry=registry, text_source=SqlTextSource(factory), executor=executor, session_factory=factory,
        audit_sink=writer, terms_for=ShoppingTerms(settings.paypal_payee_merchant_id), approval_keys=keys, provider=provider,
        light_provider=light_provider,
    )
    counterparty: SandboxCounterparty | None = None
    desk = build_desk(settings, factory)
    engagement_deps: dict[str, GraphDeps] = {}
    platforms = set(registry.connector_ids)
    if settings.sandbox_counterparties_enabled or desk is not None:
        payee = settings.paypal_payee_merchant_id
        sandbox_planner: SandboxMilestonePlanner | None = None
        terms_observer: TermsObserver | None = None
        milestone_inner: SandboxMilestoneObserver | None = None
        channel: CounterpartyChannel | None = None
        if settings.sandbox_counterparties_enabled:
            counterparty = SandboxCounterparty(factory)
            terms_observer = QuotedTermsObserver(observer, counterparty)
            registry.register(SandboxConnector(CONTRACTOR_SOURCE, CONTRACTOR.name, OFFERINGS))
            registry.register(SandboxConnector(AGENT_SOURCE, A2A.name, OFFERINGS))
            sandbox_planner, milestone_inner, channel = SandboxMilestonePlanner(payee), SandboxMilestoneObserver(), counterparty
            platforms |= set(registry.connector_ids)
        planner: MilestonePlanner | None = sandbox_planner
        milestone_observer: TermsObserver | None = milestone_inner
        if desk is not None:
            terms_observer = QuoteTermsObserver(terms_observer)
            milestone_observer = QuoteTermsObserver(milestone_inner)
            planner = QuoteMilestonePlanner(payee, sandbox_planner)
            channel = RoutedCounterparty(desk, counterparty)
            platforms.add(USER_SUPPLIED_SOURCE)
        assert terms_observer is not None and milestone_observer is not None and planner is not None
        # Contractor: a milestone plan; each milestone is approved, paid, verified and captured on its own.
        milestone_evidence = MonitorEventEvidence.for_plugin(CONTRACTOR, milestone_observer, factory)
        engagement_deps[CONTRACTOR.name] = replace(
            deps, plugin=CONTRACTOR,
            executor=Executor(payments, terms_observer, MonitorEventEvidence(terms_observer, factory), CONTRACTOR.verification),
            terms_for=PlanTerms(EngagementTerms(payee, permission="engage_contractor")),
            milestones=MilestoneFlow(
                planner=planner,
                service=MilestoneService(Executor(payments, milestone_observer, milestone_evidence, CONTRACTOR.verification), writer),
            ),
            before_settle=counterparty.fulfilment(CONTRACTOR) if counterparty is not None else None, quotes=desk,
        )
        # A2A: one payment for the whole deal; a failed delivery is renegotiated inside the user's ceiling.
        engagement_deps[A2A.name] = replace(
            deps, plugin=A2A,
            executor=Executor(payments, terms_observer, MonitorEventEvidence.for_plugin(A2A, terms_observer, factory), A2A.verification),
            terms_for=EngagementTerms(payee, permission="delegate_task"),
            envelope_for=engagement_envelope("price_ceiling"), counterparty=channel,
            before_settle=counterparty.fulfilment(A2A) if counterparty is not None else None, quotes=desk,
        )
    stop = asyncio.Event()
    workers: list[asyncio.Task[None]] = []
    background: set[asyncio.Task[object]] = set()
    run_service: RunService | None = None
    try:
        async with open_checkpointer(settings) as checkpointer:
            runs = RunController(
                build_graph(deps, checkpointer),
                by_domain={name: build_graph(d, checkpointer) for name, d in engagement_deps.items()},
            )
            if counterparty is not None:
                counterparty.bind(runs)
            if desk is not None:
                desk.bind(runs)
                app.state.counterparty_desk = desk
                app.state.twilio_auth_token = (
                    settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token else None
                )
                app.state.counterparty_inbound_secret = (
                    settings.counterparty_inbound_secret.get_secret_value() if settings.counterparty_inbound_secret else None
                )
            worker = MonitorWorker(runs, payments, factory, writer, keys, active_runs=active_run_ids(factory))
            run_service = RunService(
                runs, factory, frozenset(platforms), release_funds=worker.release_for_cancel,
                domains=frozenset({SHOPPING.name, *engagement_deps}),
                outreach_domains=frozenset(engagement_deps) if desk is not None else frozenset(),
                counterparty_required=frozenset() if settings.sandbox_counterparties_enabled else frozenset(engagement_deps),
            )
            app.state.paypal = paypal
            app.state.session_factory = factory
            app.state.runs = runs
            app.state.run_service = run_service
            app.state.monitor_dispatch = background_dispatch(worker, background)
            workers.append(
                asyncio.create_task(
                    run_sweeper(
                        worker, EventProcessor(runs.load_task), interval_seconds=SWEEP_INTERVAL_SECONDS, stop=stop, desk=desk
                    ),
                    name="deadline-sweeper",
                )
            )
            workers.append(
                asyncio.create_task(
                    run_reconciler(Reconciler(paypal, payments), factory, interval_seconds=RECONCILE_INTERVAL_SECONDS, stop=stop),
                    name="payment-reconciler",
                )
            )
            logger.info(
                "runtime ready", extra={"platforms": sorted(registry.connector_ids), "domains": sorted({SHOPPING.name, *engagement_deps})}
            )
            try:
                yield
            finally:
                # Drain everything that reads checkpoints while the checkpointer connection is still open.
                await _drain(stop, workers, background, run_service)
    finally:
        await _drain(stop, workers, background, run_service)  # no-op when already drained; covers a failed startup
        if counterparty is not None:
            await counterparty.aclose()
        if desk is not None:
            await desk.aclose()
        await paypal.aclose()
        await channel3.aclose()
        for llm in (provider, light_provider):  # OpenAI-compatible providers own an HTTP pool
            close_llm = getattr(llm, "aclose", None)
            if close_llm is not None:
                await close_llm()
