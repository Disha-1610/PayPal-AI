"""Contractor / A2A runtime wiring: sandbox counterparties, engagement terms, per-domain graphs and run intake.

The negotiation round-trip runs on the real compiled graph with the real ``SandboxCounterparty`` as the channel;
only the LLM is scripted and the payment executor is a mock (no money moves in this test)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from conftest import FakeProvider
from fastapi import FastAPI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.deps import Principal
from app.api.routes import runs as runs_routes
from app.connectors.base import CandidateRequest, SearchRequest
from app.db.base import Base
from app.db.models.monitor import MonitorEventRecord
from app.domains.a2a import A2A
from app.domains.engagement import (
    COUNTERPARTY_PARTY,
    EngagementTerms,
    engagement_envelope,
)
from app.domains.shopping_execution import MonitorEventEvidence
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.runtime import RunController, UnknownRun
from app.orchestrator.guard.approval_guard import issue_approval
from app.orchestrator.guard.negotiation_guard import proposed_terms
from app.runs.service import RunService, deadline_of, granted_actions
from app.sandbox.catalog import AGENT_SOURCE, CONTRACTOR_SOURCE, OFFERINGS, offering_for
from app.sandbox.counterparty import (
    QuotedTermsObserver,
    SandboxConnector,
    SandboxCounterparty,
)
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.candidate import Candidate
from app.schemas.monitor import TaskView
from app.schemas.negotiator import CounterOffer
from app.schemas.recheck import ObservedTerms

KEY, KEY_ID = b"k" * 32, "v1"
DEADLINE = datetime(2026, 12, 20, 23, 59, 59, tzinfo=UTC)
PAYEE = "MERCHANT-1"
SUBJECT = "sbx-ct-pixel-usd"  # listed at 900.00 USD, rejects its first delivery


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def candidate(candidate_id=SUBJECT, cost="900.00"):
    return Candidate(
        candidate_id=candidate_id, source=CONTRACTOR_SOURCE, domain="contractor", title="Pixel Forge",
        total_cost=Decimal(cost), currency="USD",
    )


def state(**extra):
    return {"run_id": "r1", "deliver_by": DEADLINE, **extra}


def terms(cost="900.00"):
    return EngagementTerms(PAYEE, permission="engage_contractor")(candidate(cost=cost), state())


# ---- catalog + connector -------------------------------------------------------------------------------

def test_every_offering_lists_both_currencies_and_round_trips_its_id():
    for offering in OFFERINGS:
        for currency in ("USD", "INR"):
            assert offering.prices[currency] > 0
            assert offering_for(offering.candidate_id(currency)) == (offering, currency)
    assert offering_for("sbx-ct-pixel-eur") is None and offering_for("unknown-usd") is None


async def test_search_returns_only_the_runs_currency_and_only_its_own_domain():
    connector = SandboxConnector(CONTRACTOR_SOURCE, "contractor", OFFERINGS)
    found = await connector.execute("search", SearchRequest(query="developer", domain="contractor", currency="INR", limit=50))
    assert found.listings and {item.candidate.currency for item in found.listings} == {"INR"}
    assert {item.candidate.source for item in found.listings} == {CONTRACTOR_SOURCE}
    agents = SandboxConnector(AGENT_SOURCE, "a2a", OFFERINGS)
    found = await agents.execute("search", SearchRequest(query="agent", domain="a2a", currency="USD", limit=50))
    assert found.listings and all(item.candidate.domain == "a2a" for item in found.listings)


async def test_a_fully_booked_contractor_reports_out_of_stock_and_completion_dates_are_in_the_future():
    connector = SandboxConnector(CONTRACTOR_SOURCE, "contractor", OFFERINGS)
    assert (await connector.execute("stock", CandidateRequest(candidate_id="sbx-ct-atlas-usd"))).available is False
    assert (await connector.execute("stock", CandidateRequest(candidate_id="sbx-ct-acme-usd"))).available is True
    listing = await connector.execute("details", CandidateRequest(candidate_id="sbx-ct-acme-usd"))
    assert listing.candidate.available_by > datetime.now(UTC)


# ---- engagement terms + envelope -------------------------------------------------------------------------

def test_engagement_terms_bind_price_deadline_payee_and_counterparty():
    approved = terms()
    assert approved.max_total_minor == approved.expected_total_minor == 90_000
    assert approved.latest_acceptable_at == DEADLINE
    assert approved.parties == {"payee_id": PAYEE, COUNTERPARTY_PARTY: CONTRACTOR_SOURCE}


def test_engagement_terms_refuse_without_a_deadline_or_a_price():
    builder = EngagementTerms(PAYEE, permission="engage_contractor")
    with pytest.raises(ValueError, match="deadline"):
        builder(candidate(), {"run_id": "r1"})
    with pytest.raises(ValueError, match="total cost"):
        builder(candidate().model_copy(update={"total_cost": None}), state())


def test_a_counterparty_change_can_never_redirect_the_payment():
    envelope = engagement_envelope("budget")(terms(), state())
    assert envelope.seller_party == COUNTERPARTY_PARTY
    rival = envelope.baseline_offer.model_copy(update={"counterparty_id": "someone-else"})
    changed = proposed_terms(envelope, rival)
    assert changed.parties["payee_id"] == PAYEE and changed.parties[COUNTERPARTY_PARTY] == "someone-else"


# ---- counterparty replies + observed price ---------------------------------------------------------------

def offer(price, subject=SUBJECT, currency="USD"):
    return CounterOffer(subject_id=subject, counterparty_id=CONTRACTOR_SOURCE, currency=currency, price_minor=price,
                        delivery_due=DEADLINE)


def test_the_counterparty_meets_halfway_below_its_price_and_accepts_at_or_above_it(factory):
    counterparty = SandboxCounterparty(factory)
    assert counterparty._reply("r1", offer(80_000))["price_minor"] == 85_000
    assert counterparty.quote("r1", SUBJECT) == 85_000
    assert counterparty._reply("r1", offer(95_000))["price_minor"] == 95_000
    assert counterparty.quote("r2", SUBJECT) is None  # quotes never leak across runs
    assert counterparty._reply("r1", offer(1, subject="not-ours-usd")) == {"decision": "decline"}


async def test_the_observed_price_is_the_counterpartys_latest_quote_for_that_run(factory):
    class Listed:
        async def observe(self, approved):
            return ObservedTerms(subject_id=approved.subject_id, currency="USD", total_minor=90_000)

    counterparty = SandboxCounterparty(factory)
    observer = QuotedTermsObserver(Listed(), counterparty)
    assert (await observer.observe(terms())).total_minor == 90_000
    counterparty._reply("r1", offer(80_000))
    assert (await observer.observe(terms())).total_minor == 85_000


# ---- delivery evidence through the Monitor engine --------------------------------------------------------

class Loader:
    def __init__(self, domain):
        self.domain = domain

    async def load_task(self, run_id):
        return TaskView(run_id=run_id, domain=self.domain)


class Echo:
    async def observe(self, approved):
        return ObservedTerms(subject_id=approved.subject_id, currency=approved.currency, parties=dict(approved.parties),
                             domain_data=dict(approved.domain_data), total_minor=approved.expected_total_minor, available=True)


async def test_an_agent_rejects_its_first_delivery_then_the_renegotiated_order_is_confirmed(factory):
    plugin, rejected, verified = A2A, "TASK_FAILED", "RESULT_ACCEPTED"
    counterparty = SandboxCounterparty(factory)
    counterparty.bind(Loader(plugin.name))  # type: ignore[arg-type]
    deliver = counterparty.fulfilment(plugin)
    evidence = MonitorEventEvidence.for_plugin(plugin, Echo(), factory)
    lingua = EngagementTerms(PAYEE, permission="delegate_task")(candidate("sbx-ag-lingua-usd", "150.00"), state())

    async def settle(order_id):
        await deliver({"order_id": order_id, "agreed_terms": lingua})
        return await evidence.collect(lingua, order_id)

    first = await settle("order-r1x-abc")
    again = await settle("order-r1x-abc")  # idempotent: one report per order and outcome
    second = await settle("order-r1x-abc-r1")
    assert (first.confirmed, again.confirmed, second.confirmed) == (False, False, True)
    async with factory() as session:
        rows = (await session.execute(select(MonitorEventRecord))).scalars().all()
    assert {(r.entity_id, r.event_type, r.status, r.source) for r in rows} == {
        ("order-r1x-abc", rejected, "applied", "sandbox"), ("order-r1x-abc-r1", verified, "applied", "sandbox"),
    }


async def test_a_delivering_agent_is_confirmed_once_it_reports_the_work(factory):
    counterparty = SandboxCounterparty(factory)
    counterparty.bind(Loader("a2a"))  # type: ignore[arg-type]
    evidence = MonitorEventEvidence.for_plugin(A2A, Echo(), factory)
    scout = EngagementTerms(PAYEE, permission="delegate_task")(candidate("sbx-ag-scout-usd", "800.00"), state())
    assert (await evidence.collect(scout, "order-r1-def")).confirmed is None  # nothing reported yet: not confirmed
    await counterparty.fulfilment(A2A)({"order_id": "order-r1-def", "agreed_terms": scout})
    assert (await evidence.collect(scout, "order-r1-def")).confirmed is True


# ---- the real graph: counter -> sandbox reply -> accept -> fresh user approval ---------------------------

def decision(action, price=None):
    body = {"action": action, "rationale": "because"}
    if action == "counter":
        body["counter"] = {"price_minor": price, "delivery_due": DEADLINE.isoformat(), "terms": {}}
    return json.dumps(body)


async def test_negotiation_round_trip_with_the_sandbox_counterparty_on_the_compiled_graph(factory):
    counterparty = SandboxCounterparty(factory)
    executor = MagicMock()
    executor.place_order = AsyncMock(return_value=SimpleNamespace(
        created=SimpleNamespace(payment=SimpleNamespace(id="00000000-0000-0000-0000-000000000001"), approval_url="https://pay/x")
    ))
    deps = GraphDeps(
        plugin=A2A, registry=MagicMock(), text_source=MagicMock(), executor=executor, session_factory=factory,
        audit_sink=MagicMock(), terms_for=MagicMock(), approval_keys={KEY_ID: KEY},
        provider=FakeProvider(decision("counter", 80_000), decision("accept")),
        envelope_for=engagement_envelope("budget"), counterparty=counterparty,
    )
    graph = build_graph(deps, MemorySaver())
    runs = RunController(MagicMock(), by_domain={"a2a": graph})
    runs._graph = graph  # reads go through any domain graph; here the only one
    counterparty.bind(runs)
    auth = TaskAuthorization(
        run_id="r1", max_spend=Decimal(5000), currency="USD", permitted_platforms=frozenset({CONTRACTOR_SOURCE}),
        permitted_actions=granted_actions("a2a"),
    )
    approved = terms()
    approvals = [
        issue_approval(terms=approved, run_id="r1", authorization_id=auth.authorization_id, action=action, candidate_id=SUBJECT,
                       platform=CONTRACTOR_SOURCE, approved_by="u1", session_id="s1", key=KEY, key_id=KEY_ID, ttl=timedelta(minutes=10))
        for action in sorted(auth.approval_required_actions, key=lambda a: a.value)
    ]
    config = {"configurable": {"thread_id": "r1"}}
    await graph.aupdate_state(config, {
        "run_id": "r1", "goal": "translate our docs", "domain": "a2a", "status": "voided", "authorization": auth,
        "original_intent": "x", "approvals": approvals, "deliver_by": DEADLINE,
    }, as_node="settle")

    await graph.ainvoke(None, config)
    assert (await graph.aget_state(config)).next == ("await_counterparty",)
    await counterparty.aclose()  # the sandbox reply is delivered and the run resumes through the Guard

    done = await graph.aget_state(config)
    assert done.next == ("await_approval",)
    pending = done.values["pending_approval"].terms
    assert pending.max_total_minor == pending.expected_total_minor == 85_000  # met halfway between 800 and 900
    assert pending.parties["payee_id"] == PAYEE
    assert counterparty.quote("r1", SUBJECT) == 85_000

    # The user approves the renegotiated deal: the order must use THIS approval, never the original one.
    fresh = [
        issue_approval(terms=pending, run_id="r1", authorization_id=auth.authorization_id, action=action, candidate_id=SUBJECT,
                       platform=CONTRACTOR_SOURCE, approved_by="u1", session_id="s2", key=KEY, key_id=KEY_ID, ttl=timedelta(minutes=10))
        for action in sorted(auth.approval_required_actions, key=lambda a: a.value)
    ]
    await graph.ainvoke(Command(resume={"decision": "approve", "approvals": [a.model_dump(mode="json") for a in fresh]}), config)
    assert (await graph.aget_state(config)).next == ("await_payer",)
    placed = executor.place_order.await_args.kwargs
    assert placed["approval"].terms_hash == pending.terms_hash() != approved.terms_hash()
    assert {a.terms_hash for a in placed["permit"].approvals} == {pending.terms_hash()}
    assert placed["payee_id"] == PAYEE


# ---- intake: domains, deadlines and grants ---------------------------------------------------------------

class FakeController:
    def __init__(self):
        self.started = []

    async def snapshot(self, run_id):
        raise UnknownRun(run_id)

    async def start(self, initial):
        self.started.append(initial)
        return {"status": "running"}


def client_for(factory, domains):
    controller = FakeController()
    service = RunService(controller, factory, frozenset({"channel3", CONTRACTOR_SOURCE}), domains=domains)  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(runs_routes.router)
    app.state.run_service = service

    async def authenticate(request):
        return Principal(user_id="u1", session_id="s1")

    app.state.authenticator = authenticate
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), service, controller


BODY = {"goal": "redesign our landing page in React", "max_spend": 1000, "currency": "USD", "domain": "contractor"}


async def test_a_contractor_run_starts_its_domain_with_the_deadline_and_the_negotiate_grant(factory):
    client, service, controller = client_for(factory, frozenset({"shopping", "contractor", "a2a"}))
    response = await client.post("/runs", json={**BODY, "deliver_by": "2026-12-20"}, headers={"Idempotency-Key": "key-0000001"})
    assert response.status_code == 201
    await service.aclose()
    (initial,) = controller.started
    assert initial["domain"] == "contractor" and initial["deliver_by"] == DEADLINE
    assert Action.NEGOTIATE in initial["authorization"].permitted_actions
    assert (await client.get(f"/runs/{response.json()['run_id']}")).json()["domain"] == "contractor"


async def test_engagement_runs_need_a_deadline_and_an_enabled_domain(factory):
    client, _, controller = client_for(factory, frozenset({"shopping"}))
    missing = await client.post("/runs", json=BODY, headers={"Idempotency-Key": "key-0000001"})
    disabled = await client.post("/runs", json={**BODY, "deliver_by": "2026-12-20"}, headers={"Idempotency-Key": "key-0000002"})
    unknown = await client.post("/runs", json={**BODY, "domain": "travel"}, headers={"Idempotency-Key": "key-0000003"})
    assert (missing.status_code, disabled.status_code, unknown.status_code) == (422, 422, 422)
    assert "not available" in disabled.json()["detail"] and controller.started == []


def test_shopping_keeps_least_privilege_and_bare_dates_mean_end_of_day_utc():
    assert Action.NEGOTIATE not in granted_actions("shopping")
    assert Action.NEGOTIATE in granted_actions("a2a")
    assert deadline_of(datetime(2026, 12, 20)) == DEADLINE
    aware = datetime(2026, 12, 20, 9, tzinfo=UTC)
    assert deadline_of(aware) == aware and deadline_of(None) is None


# ---- RunController: each run is driven by its own domain's graph -------------------------------------------

class Graph:
    def __init__(self, name, domain_of=None):
        self.name, self.domain_of, self.calls = name, domain_of or {}, []

    async def aget_state(self, config):
        domain = self.domain_of.get(config["configurable"]["thread_id"])
        return MagicMock(values={"domain": domain} if domain else {}, next=(), tasks=())

    async def ainvoke(self, payload, config):
        self.calls.append(("invoke", config["configurable"]["thread_id"]))
        return {"status": self.name}

    async def aupdate_state(self, config, values, as_node=None):
        self.calls.append(("update", config["configurable"]["thread_id"]))


async def test_runs_start_resume_and_cancel_on_their_own_domain_graph():
    default = Graph("shopping", {"c1": "contractor", "s1": "shopping"})
    contractor = Graph("contractor")
    runs = RunController(default, by_domain={"contractor": contractor})
    assert (await runs.start({"run_id": "c1", "domain": "contractor"}))["status"] == "contractor"
    assert (await runs.start({"run_id": "s1", "domain": "shopping"}))["status"] == "shopping"
    assert (await runs.resume("c1", {"x": 1}))["status"] == "contractor"
    await runs.cancel("c1")
    assert contractor.calls == [("invoke", "c1"), ("invoke", "c1"), ("update", "c1")]
    assert default.calls == [("invoke", "s1")]
