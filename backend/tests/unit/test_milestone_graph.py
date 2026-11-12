"""Contractor milestones on the real compiled graph, end to end.

Real: graph, guards, approval nodes, ``MilestoneService``, ``Executor``, ``PaymentService`` ledger, connector registry,
sandbox planner / observer / evidence and the Monitor event engine. Fake: only the PayPal provider (in memory) and the
LLM (never called on this path). Approvals are minted exactly as the approval API does: over the server-side pending
terms, signed with the approval key.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.base import CandidateRequest
from app.connectors.registry import ConnectorRegistry
from app.db.base import Base
from app.db.models.milestone import Milestone, MilestoneStatus
from app.db.models.payment import Payment
from app.db.repositories.connector_text import SqlTextStore
from app.domains.contractor import CONTRACTOR
from app.domains.contractor.milestone import MilestoneService
from app.domains.engagement import EngagementTerms, PlanTerms
from app.domains.shopping_execution import ConnectorTermsObserver, MonitorEventEvidence
from app.orchestrator.executor.executor import Executor
from app.orchestrator.executor.mismatch import classify_mismatch
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.milestones import MilestoneFlow
from app.orchestrator.graph.runtime import RunController
from app.orchestrator.guard.approval_guard import issue_approval
from app.payments.service import PaymentService
from app.runs.service import granted_actions
from app.sandbox.catalog import CONTRACTOR_SOURCE, OFFERINGS
from app.sandbox.counterparty import (
    QuotedTermsObserver,
    SandboxConnector,
    SandboxCounterparty,
    SandboxMilestoneObserver,
    SandboxMilestonePlanner,
)
from app.schemas.authorization import TaskAuthorization
from app.schemas.recheck import MismatchAction
from tests.unit.payment_fakes import FakeProvider

KEY, KEY_ID, PAYEE, RUN = b"k" * 32, "v1", "MERCHANT-1", "r1"
CONFIG = {"configurable": {"thread_id": RUN}}


class Provider(FakeProvider):
    """One in-memory PayPal order per milestone: each new order starts unapproved."""

    async def create_order(self, *, request_id, amount, currency, payee_id, reference, run_id=None):
        if f"create:{request_id}" not in self.seen:
            self.order_status = "created"
        return await super().create_order(
            request_id=request_id, amount=amount, currency=currency, payee_id=payee_id, reference=reference
        )


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'g.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def start(factory, candidate_id: str):
    provider = Provider()
    payments = PaymentService(provider)
    registry = ConnectorRegistry(SqlTextStore(factory))
    connector = SandboxConnector(CONTRACTOR_SOURCE, "contractor", OFFERINGS)
    registry.register(connector)
    counterparty = SandboxCounterparty(factory)
    quoted = QuotedTermsObserver(ConnectorTermsObserver(registry), counterparty)
    milestone_observer = SandboxMilestoneObserver()
    evidence = MonitorEventEvidence.for_plugin(CONTRACTOR, milestone_observer, factory)
    deps = GraphDeps(
        plugin=CONTRACTOR, registry=registry, text_source=MagicMock(), session_factory=factory, audit_sink=MagicMock(),
        executor=Executor(payments, quoted, MonitorEventEvidence(quoted, factory), CONTRACTOR.verification),
        terms_for=PlanTerms(EngagementTerms(PAYEE, permission="engage_contractor")), approval_keys={KEY_ID: KEY},
        milestones=MilestoneFlow(
            planner=SandboxMilestonePlanner(PAYEE),
            service=MilestoneService(Executor(payments, milestone_observer, evidence, CONTRACTOR.verification), MagicMock()),
        ),
        before_settle=counterparty.fulfilment(CONTRACTOR),
    )
    graph = build_graph(deps, MemorySaver())
    counterparty.bind(RunController(graph))
    auth = TaskAuthorization(
        run_id=RUN, max_spend=Decimal(5000), currency="USD", permitted_platforms=frozenset({CONTRACTOR_SOURCE}),
        permitted_actions=granted_actions("contractor"),
    )
    listing = await connector.execute("details", CandidateRequest(candidate_id=candidate_id))
    await graph.aupdate_state(CONFIG, {
        "run_id": RUN, "original_intent": "build our site", "goal": "build our site", "domain": "contractor",
        "status": "selected", "authorization": auth, "candidates": [listing.candidate], "selected_candidate_id": candidate_id,
        "deliver_by": datetime.now(UTC) + timedelta(days=30),
    }, as_node="select")
    await graph.ainvoke(None, CONFIG)
    return graph, provider, auth


async def paused(graph):
    return (await graph.aget_state(CONFIG)).next


async def approve(graph, auth):
    """What ``POST /runs/{id}/approval`` does: sign the server-side pending terms, then resume."""
    pending = (await graph.aget_state(CONFIG)).values["pending_approval"]
    approvals = [
        issue_approval(
            terms=pending.terms, run_id=RUN, authorization_id=auth.authorization_id, action=action,
            candidate_id=pending.candidate_id, platform=pending.platform, approved_by="u1", session_id="s1", key=KEY,
            key_id=KEY_ID, ttl=timedelta(minutes=10),
        )
        for action in sorted(auth.approval_required_actions, key=lambda a: a.value)
    ]
    await graph.ainvoke(Command(resume={"decision": "approve", "approvals": [a.model_dump(mode="json") for a in approvals]}), CONFIG)
    return pending.terms


async def payer_returns(graph, provider):
    provider.order_status = "payer_approved"
    await graph.ainvoke(Command(resume={"payer_returned": True}), CONFIG)


async def rows(factory):
    async with factory() as session:
        return list((await session.execute(select(Milestone).order_by(Milestone.sequence))).scalars())


async def payments(factory):
    async with factory() as session:
        return list((await session.execute(select(Payment).order_by(Payment.created_at))).scalars())


async def test_plan_then_each_milestone_is_approved_paid_verified_and_captured_in_order(factory):
    graph, provider, auth = await start(factory, "sbx-ct-acme-usd")  # 1200.00 USD, 30/50/20 plan
    assert await paused(graph) == ("await_approval",)
    plan_terms = await approve(graph, auth)
    assert plan_terms.max_total_minor == 120_000 and "plan_hash" in plan_terms.domain_data

    captured = []
    for sequence, amount in ((1, 36_000), (2, 60_000), (3, 24_000)):
        assert await paused(graph) == ("await_approval",)
        milestone_terms = await approve(graph, auth)
        assert milestone_terms.subject_id == f"sbx-ct-acme-usd:{sequence}" and milestone_terms.max_total_minor == amount
        assert milestone_terms.parties["payee_id"] == PAYEE
        assert await paused(graph) == ("await_payer",)
        assert [r.status for r in await rows(factory)][sequence:] == ["pending"] * (3 - sequence)  # later ones wait
        await payer_returns(graph, provider)
        captured.append(Decimal(amount) / 100)

    final = await graph.aget_state(CONFIG)
    assert final.next == () and final.values["status"] == "completed"
    assert [r.status for r in await rows(factory)] == [MilestoneStatus.SETTLED.value] * 3
    ledger = await payments(factory)
    assert [p.status for p in ledger] == ["captured"] * 3 and [p.amount for p in ledger] == captured
    assert sum(captured) == Decimal("1200.00")


async def test_a_rejected_milestone_is_voided_and_stops_the_run_without_starting_later_ones(factory):
    graph, provider, auth = await start(factory, "sbx-ct-pixel-usd")  # 900.00 USD; rejects milestone 2
    await approve(graph, auth)  # the plan
    await approve(graph, auth)  # milestone 1
    await payer_returns(graph, provider)
    await approve(graph, auth)  # milestone 2
    await payer_returns(graph, provider)

    final = await graph.aget_state(CONFIG)
    assert final.next == () and final.values["status"] == "stopped" and final.values["stop_reason"] == "milestone_failed"
    assert any("milestone 2 was voided" in e and "1 of 3 milestones were paid" in e for e in final.values["errors"])
    assert [r.status for r in await rows(factory)] == ["settled", "voided", "pending"]
    assert [(p.status, p.amount) for p in await payments(factory)] == [("captured", Decimal("270.00")), ("voided", Decimal("450.00"))]


async def test_rejecting_the_plan_stops_before_anything_is_stored_or_paid(factory):
    graph, _, _ = await start(factory, "sbx-ct-acme-usd")
    await graph.ainvoke(Command(resume={"decision": "reject"}), CONFIG)
    final = await graph.aget_state(CONFIG)
    assert final.next == () and final.values["status"] == "stopped"
    assert await rows(factory) == [] and await payments(factory) == []


async def test_plans_never_pass_the_deadline_and_a_late_contractor_is_caught_at_the_recheck(factory):
    registry = ConnectorRegistry(SqlTextStore(factory))
    connector = SandboxConnector(CONTRACTOR_SOURCE, "contractor", OFFERINGS)
    registry.register(connector)
    deadline = datetime.now(UTC) + timedelta(days=3)  # AppCraft needs 14 days
    listing = await connector.execute("details", CandidateRequest(candidate_id="sbx-ct-mobile-usd"))
    state = {"run_id": RUN, "deliver_by": deadline}
    plan = await SandboxMilestonePlanner(PAYEE).propose(listing.candidate, state)
    assert all(m.due_date <= deadline for m in plan.milestones)
    assert sum(m.amount for m in plan.milestones) == listing.candidate.total_cost == plan.budget

    terms = PlanTerms(EngagementTerms(PAYEE, permission="engage_contractor"))(listing.candidate, {**state, "milestone_plan": plan})
    observed = await ConnectorTermsObserver(registry).observe(terms)
    assert classify_mismatch(terms, observed).action is MismatchAction.DEADLINE_MISSED


async def test_the_planner_refuses_unknown_work_and_a_missing_or_past_deadline():
    planner = SandboxMilestonePlanner(PAYEE)
    connector = SandboxConnector(CONTRACTOR_SOURCE, "contractor", OFFERINGS)
    acme = (await connector.execute("details", CandidateRequest(candidate_id="sbx-ct-acme-usd"))).candidate
    with pytest.raises(ValueError, match="deadline"):
        await planner.propose(acme, {"run_id": RUN})
    with pytest.raises(ValueError, match="already passed"):
        await planner.propose(acme, {"run_id": RUN, "deliver_by": datetime.now(UTC) - timedelta(days=1)})
    with pytest.raises(ValueError, match="not a sandbox contractor"):
        await planner.propose(acme.model_copy(update={"candidate_id": "elsewhere-usd"}), {"run_id": RUN, "deliver_by": datetime.now(UTC) + timedelta(days=9)})


async def test_the_run_view_shows_the_proposed_plan_then_each_milestones_progress(factory):
    from app.runs.view import milestone_views

    graph, provider, auth = await start(factory, "sbx-ct-acme-usd")
    plan = (await graph.aget_state(CONFIG)).values["milestone_plan"]
    proposed = milestone_views([], plan)
    assert [(m.sequence, m.amount, m.status) for m in proposed] == [(1, 360.0, "proposed"), (2, 600.0, "proposed"), (3, 240.0, "proposed")]
    await approve(graph, auth)  # plan
    await approve(graph, auth)  # milestone 1
    await payer_returns(graph, provider)
    shown = milestone_views(await rows(factory), plan)
    assert [(m.sequence, m.status, m.currency) for m in shown] == [(1, "settled", "USD"), (2, "pending", "USD"), (3, "pending", "USD")]
    assert milestone_views([], None) is None
