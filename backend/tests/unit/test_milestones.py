from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.audit import AuditLog
from app.db.models.milestone import Milestone, MilestoneStatus, VerificationStatus
from app.db.models.payment import Payment
from app.domains.contractor.milestone import (
    MilestoneOutOfOrder,
    MilestonePlanChanged,
    MilestoneService,
    terms_for,
)
from app.domains.contractor.schemas import MilestonePlan, MilestoneSpec
from app.orchestrator.executor.executor import ExecutionBlocked, Executor
from app.orchestrator.executor.state import ExecutionState
from app.orchestrator.guard.approval_guard import issue_approval, signed_approval_verifier
from app.payments.service import PaymentNotReady, PaymentPermit, PaymentService
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.recheck import ObservedTerms
from app.schemas.verification import Evidence, VerificationSpec
from tests.unit.payment_fakes import FakeProvider

KEY = b"k" * 32
RUN = "r1"
PROJECT = "p1"
START = datetime(2026, 12, 1, tzinfo=UTC)
ALL_MONEY = (Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT)
AUTH = TaskAuthorization(
    authorization_id="a1", run_id=RUN, max_spend=Decimal(6000), permitted_platforms=frozenset({"work"}),
    permitted_actions=frozenset({*ALL_MONEY, Action.VOID_PAYMENT}),
)


def spec(amount: int, days: int, criteria: tuple[str, ...] = ("tests pass",)) -> MilestoneSpec:
    return MilestoneSpec(
        description=f"deliverable {days}", amount=Decimal(amount), due_date=START + timedelta(days=days), acceptance_criteria=criteria
    )


def make_plan(**kw) -> MilestonePlan:
    base = {
        "project_id": PROJECT, "contractor_id": "c1", "currency": "INR", "budget": Decimal(6000),
        "milestones": (spec(2000, 10), spec(3000, 20)),
    }
    return MilestonePlan(**{**base, **kw})


class AuditSink:
    def __init__(self) -> None:
        self.events = []

    def enqueue(self, event) -> None:
        self.events.append(event)


class Observer:
    async def observe(self, terms):
        return ObservedTerms(
            available=True, subject_id=terms.subject_id, parties=terms.parties, currency=terms.currency,
            total_minor=terms.expected_total_minor, available_by=terms.latest_acceptable_at - timedelta(days=1),
            permissions=terms.permissions, domain_data=terms.domain_data,
        )


class Source:
    """Evaluator evidence per milestone subject: True = deliverable accepted, False = rejected, None = pending."""

    def __init__(self) -> None:
        self.confirmed: dict[str, bool | None] = {}

    async def collect(self, terms, order_id):
        observed = await Observer().observe(terms)
        return Evidence(source="evaluator", observed=observed, confirmed=self.confirmed.get(terms.subject_id))


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def world():
    provider, source, sink = FakeProvider(), Source(), AuditSink()
    executor = Executor(PaymentService(provider), Observer(), source, VerificationSpec(max_attempts=2))
    return {"provider": provider, "source": source, "sink": sink, "service": MilestoneService(executor, sink)}


def permit_for(row: Milestone, *, other: Milestone | None = None) -> tuple[PaymentPermit, dict[Action, object]]:
    """A permit whose approvals are signed over ``row``'s terms (or ``other``'s, to test a wrong approval)."""
    signed_terms = terms_for(other or row)
    approvals = {
        action: issue_approval(
            terms=signed_terms, run_id=RUN, authorization_id="a1", action=action, candidate_id=signed_terms.subject_id,
            platform="work", approved_by="u1", session_id="s1", key=KEY, key_id="v1", ttl=timedelta(minutes=10),
        )
        for action in ALL_MONEY
    }
    verifier = signed_approval_verifier(
        current_terms=terms_for(row), keys={"v1": KEY}, run_id=RUN, session_id="s1", approved_by="u1"
    )
    return PaymentPermit(AUTH, "work", tuple(approvals.values()), verifier), approvals


async def create(factory, w, plan=None) -> list[Milestone]:
    plan = plan or make_plan()
    async with factory() as s, s.begin():
        return await w["service"].create_plan(s, run_id=RUN, plan=plan, approved_plan_hash=plan.plan_hash())


async def row_at(factory, sequence: int) -> Milestone:
    async with factory() as s:
        return (await s.execute(select(Milestone).where(Milestone.sequence == sequence))).scalar_one()


async def start(factory, w, sequence: int):
    row = await row_at(factory, sequence)
    permit, approvals = permit_for(row)
    async with factory() as s, s.begin():
        return await w["service"].start(
            s, run_id=RUN, project_id=PROJECT, sequence=sequence, permit=permit, approval=approvals[Action.CREATE_ORDER]
        )


async def settle(factory, w, sequence: int):
    row = await row_at(factory, sequence)
    permit, _ = permit_for(row)
    w["provider"].order_status = "payer_approved"
    async with factory() as s, s.begin():
        return await w["service"].settle(s, run_id=RUN, project_id=PROJECT, sequence=sequence, permit=permit)


# ---- plan schema ------------------------------------------------------------------------------


def test_plan_rejects_total_above_budget_backwards_dates_blank_criteria_and_naive_dates():
    with pytest.raises(ValidationError, match="exceeds the budget"):
        make_plan(budget=Decimal(4999))
    with pytest.raises(ValidationError, match="backwards"):
        make_plan(milestones=(spec(1000, 20), spec(1000, 10)))
    with pytest.raises(ValidationError):
        spec(1000, 10, criteria=("  ",))
    with pytest.raises(ValidationError):
        spec(1000, 10, criteria=())
    with pytest.raises(ValidationError, match="timezone"):
        MilestoneSpec(description="x", amount=Decimal(1), due_date=datetime(2026, 12, 1), acceptance_criteria=("ok",))


@pytest.mark.parametrize(
    "changed",
    [
        {"milestones": (spec(2100, 10), spec(3000, 20))},  # amount
        {"milestones": (spec(2000, 12), spec(3000, 20))},  # due date
        {"milestones": (spec(2000, 10, ("tests pass", "no TODOs")), spec(3000, 20))},  # criteria
        {"contractor_id": "c2"},  # payee
    ],
)
def test_any_change_to_the_plan_changes_its_hash(changed):
    assert make_plan(**changed).plan_hash() != make_plan().plan_hash()


# ---- persistence ------------------------------------------------------------------------------


async def test_create_plan_persists_each_milestone_with_its_own_terms_hash(factory, world):
    rows = await create(factory, world)
    assert [(r.sequence, r.amount, r.status) for r in rows] == [(1, Decimal(2000), "pending"), (2, Decimal(3000), "pending")]
    assert rows[0].terms_hash != rows[1].terms_hash
    assert all(r.verification_status == VerificationStatus.PENDING.value and r.payment_id is None for r in rows)


async def test_create_plan_is_idempotent_for_the_same_plan(factory, world):
    first = await create(factory, world)
    second = await create(factory, world)
    assert [r.id for r in first] == [r.id for r in second]


async def test_plan_that_differs_from_the_approved_hash_is_rejected_and_audited(factory, world):
    approved = make_plan()
    inflated = make_plan(milestones=(spec(2500, 10), spec(3000, 20)))
    with pytest.raises(MilestonePlanChanged):
        async with factory() as s, s.begin():
            await world["service"].create_plan(s, run_id=RUN, plan=inflated, approved_plan_hash=approved.plan_hash())
    async with factory() as s:
        assert (await s.execute(select(Milestone))).first() is None
    assert [(e.action, e.decision, e.phase) for e in world["sink"].events] == [("MILESTONE_PLAN_REJECTED", "deny", "domain")]


async def test_a_second_different_plan_for_an_approved_project_is_rejected(factory, world):
    await create(factory, world)
    other = make_plan(milestones=(spec(1000, 10), spec(1000, 20)))
    with pytest.raises(MilestonePlanChanged):
        await create(factory, world, other)


# ---- lifecycle --------------------------------------------------------------------------------


async def test_each_milestone_is_authorized_verified_and_captured_in_order(factory, world):
    await create(factory, world)
    world["source"].confirmed = {f"{PROJECT}:1": True, f"{PROJECT}:2": True}

    placed = await start(factory, world, 1)
    assert placed.state is ExecutionState.AWAITING_CONFIRMATION
    first = await settle(factory, world, 1)
    assert first.state is ExecutionState.SETTLED and first.captured == Decimal(2000)

    async with factory() as s:
        assert (await world["service"].is_complete(s, RUN, PROJECT)) is False
    await start(factory, world, 2)
    second = await settle(factory, world, 2)
    assert second.captured == Decimal(3000)

    async with factory() as s:
        assert await world["service"].is_complete(s, RUN, PROJECT) is True
        rows = await world["service"].milestones(s, RUN, PROJECT)
        payments = (await s.execute(select(Payment))).scalars().all()
        actions = [a for (a,) in (await s.execute(select(AuditLog.action).where(AuditLog.phase == "domain"))).all()]
    assert [(r.status, r.verification_status) for r in rows] == [("settled", "passed")] * 2
    assert len({r.payment_id for r in rows}) == 2 and {p.status for p in payments} == {"captured"}
    assert {p.amount for p in payments} == {Decimal(2000), Decimal(3000)}
    assert {"MILESTONE_CREATED", "MILESTONE_STARTED", "MILESTONE_SETTLED", "PROJECT_COMPLETED"} <= set(actions)


async def test_a_later_milestone_cannot_start_before_the_earlier_one_settled(factory, world):
    await create(factory, world)
    with pytest.raises(MilestoneOutOfOrder):
        await start(factory, world, 2)
    assert not world["provider"].calls
    await start(factory, world, 1)
    with pytest.raises(MilestoneOutOfOrder):  # order placed but not yet verified/captured
        await start(factory, world, 2)


async def test_failed_verification_voids_that_milestone_and_blocks_the_rest(factory, world):
    await create(factory, world)
    world["source"].confirmed = {f"{PROJECT}:1": False}
    await start(factory, world, 1)
    result = await settle(factory, world, 1)
    assert result.state is ExecutionState.VOIDED
    row = await row_at(factory, 1)
    assert (row.status, row.verification_status) == (MilestoneStatus.VOIDED.value, VerificationStatus.FAILED.value)
    async with factory() as s:
        assert (await s.get(Payment, row.payment_id)).status == "voided"
    with pytest.raises(MilestoneOutOfOrder):
        await start(factory, world, 2)
    assert "capture" not in [name for name, _ in world["provider"].calls]


async def test_pending_evidence_keeps_the_hold_then_expires_after_the_attempt_limit(factory, world):
    await create(factory, world)
    await start(factory, world, 1)
    waiting = await settle(factory, world, 1)
    assert waiting.state is ExecutionState.AWAITING_CONFIRMATION
    assert (await row_at(factory, 1)).status == MilestoneStatus.ORDER_PLACED.value
    expired = await settle(factory, world, 1)
    assert expired.state is ExecutionState.EXPIRED
    assert (await row_at(factory, 1)).status == MilestoneStatus.EXPIRED.value


async def test_settle_before_the_payer_approves_changes_nothing(factory, world):
    await create(factory, world)
    await start(factory, world, 1)
    row = await row_at(factory, 1)
    permit, _ = permit_for(row)
    with pytest.raises(PaymentNotReady):
        async with factory() as s, s.begin():
            await world["service"].settle(s, run_id=RUN, project_id=PROJECT, sequence=1, permit=permit)
    after = await row_at(factory, 1)
    assert (after.status, after.attempts) == (MilestoneStatus.ORDER_PLACED.value, 0)


# ---- tampering --------------------------------------------------------------------------------


async def test_a_milestone_edited_after_approval_cannot_start(factory, world):
    await create(factory, world)
    async with factory() as s, s.begin():
        await s.execute(update(Milestone).where(Milestone.sequence == 1).values(amount=Decimal(2500)))
    with pytest.raises(MilestonePlanChanged):
        await start(factory, world, 1)
    assert not world["provider"].calls


async def test_changed_acceptance_criteria_after_approval_cannot_start(factory, world):
    await create(factory, world)
    async with factory() as s, s.begin():
        await s.execute(update(Milestone).where(Milestone.sequence == 1).values(acceptance_criteria=["anything goes"]))
    with pytest.raises(MilestonePlanChanged):
        await start(factory, world, 1)


async def test_an_approval_for_another_milestone_cannot_start_this_one(factory, world):
    await create(factory, world)
    first, second = await row_at(factory, 1), await row_at(factory, 2)
    permit, approvals = permit_for(first, other=second)
    with pytest.raises(ExecutionBlocked):
        async with factory() as s, s.begin():
            await world["service"].start(
                s, run_id=RUN, project_id=PROJECT, sequence=1, permit=permit, approval=approvals[Action.CREATE_ORDER]
            )
    assert not world["provider"].calls
