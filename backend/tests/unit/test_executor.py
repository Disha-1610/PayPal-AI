from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.payment import Payment
from app.orchestrator.agents.verifier import verify_evidence
from app.orchestrator.executor.executor import ExecutionBlocked, Executor
from app.orchestrator.executor.state import (
    TERMINAL,
    TRANSITIONS,
    ExecutionState,
    InvalidExecutionTransition,
    advance,
)
from app.orchestrator.guard.approval_guard import (
    issue_approval,
    signed_approval_verifier,
)
from app.payments.provider import ProviderOrder
from app.payments.service import PaymentNotReady, PaymentPermit, PaymentService
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.recheck import MismatchAction, ObservedTerms
from app.schemas.verification import Evidence, VerificationSpec, VerificationVerdict
from tests.unit.payment_fakes import FakeProvider

KEY = b"k" * 32
DEADLINE = datetime(2026, 11, 20, tzinfo=UTC)
V = VerificationVerdict
TERMS = ApprovalTerms(
    subject_id="shoe-1", subject_attributes={"size": "M"}, parties={"payee_id": "m1"}, currency="INR",
    max_total_minor=550000, expected_total_minor=520000, latest_acceptable_at=DEADLINE, permissions=frozenset({"purchase"}),
)
ALL_MONEY = (Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT)
AUTH = TaskAuthorization(
    authorization_id="a1", run_id="r1", max_spend=Decimal(6000), permitted_platforms=frozenset({"shop"}),
    permitted_actions=frozenset({*ALL_MONEY, Action.VOID_PAYMENT}),
)


def observed(**kw) -> ObservedTerms:
    base = {
        "available": True, "subject_id": "shoe-1", "subject_attributes": {"size": "M"}, "parties": {"payee_id": "m1"},
        "currency": "INR", "total_minor": 520000, "available_by": DEADLINE - timedelta(days=3),
        "permissions": frozenset({"purchase"}),
    }
    return ObservedTerms(**{**base, **kw})


class Observer:
    def __init__(self) -> None:
        self.current = observed()

    async def observe(self, terms):
        return self.current


class Source:
    def __init__(self) -> None:
        self.evidence = Evidence(source="shop", observed=observed(), confirmed=True)

    async def collect(self, terms, order_id):
        return self.evidence


def approvals():
    return {
        a: issue_approval(
            terms=TERMS, run_id="r1", authorization_id="a1", action=a, candidate_id="shoe-1", platform="shop",
            approved_by="u1", session_id="s1", key=KEY, key_id="v1", ttl=timedelta(minutes=10),
        )
        for a in ALL_MONEY
    }


def make_permit(approved) -> PaymentPermit:
    verifier = signed_approval_verifier(current_terms=TERMS, keys={"v1": KEY}, run_id="r1", session_id="s1", approved_by="u1")
    return PaymentPermit(AUTH, "shop", tuple(approved.values()), verifier)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def world():
    approved = approvals()
    provider, observer, source = FakeProvider(), Observer(), Source()
    executor = Executor(PaymentService(provider), observer, source, VerificationSpec(max_attempts=3))
    return {
        "approved": approved, "permit": make_permit(approved), "provider": provider, "observer": observer,
        "source": source, "executor": executor,
    }


async def place(factory, w):
    async with factory() as s, s.begin():
        return await w["executor"].place_order(
            s, permit=w["permit"], approval=w["approved"][Action.CREATE_ORDER], terms=TERMS, order_id="o1", payee_id="m1"
        )


async def settle(factory, w, payment_id, attempt=1):
    async with factory() as s, s.begin():
        return await w["executor"].settle(s, payment_id, permit=w["permit"], terms=TERMS, order_id="o1", attempt=attempt)


async def ledger(factory, payment_id):
    async with factory() as s:
        return (await s.get(Payment, payment_id)).status


# ---- state machine / verifier (pure) ----------------------------------------------------------


def test_terminal_states_have_no_exits_and_invalid_jumps_are_rejected():
    assert all(TRANSITIONS[state] == frozenset() for state in TERMINAL)
    assert advance(ExecutionState.PROPOSED, ExecutionState.RECHECKED) is ExecutionState.RECHECKED
    with pytest.raises(InvalidExecutionTransition):
        advance(ExecutionState.PROPOSED, ExecutionState.SETTLED)
    with pytest.raises(InvalidExecutionTransition):
        advance(ExecutionState.COMPLETED, ExecutionState.PROPOSED)


@pytest.mark.parametrize(
    ("evidence", "verdict"),
    [
        (Evidence(source="s", observed=observed(), confirmed=True), V.PASS),
        (Evidence(source="s", observed=observed(total_minor=510000), confirmed=True), V.PASS),  # cheaper is fine
        (Evidence(source="s", observed=observed(), confirmed=None), V.INSUFFICIENT_EVIDENCE),
        (Evidence(source="s", observed=observed(total_minor=None), confirmed=True), V.INSUFFICIENT_EVIDENCE),
        (Evidence(source="s", observed=observed(), confirmed=False), V.FAIL),
        (Evidence(source="s", observed=observed(total_minor=560000), confirmed=True), V.FAIL),
        (Evidence(source="s", observed=observed(available_by=DEADLINE + timedelta(days=1)), confirmed=True), V.FAIL),
        (Evidence(source="s", observed=observed(subject_attributes={"size": "L"}), confirmed=True), V.FAIL),
        (Evidence(source="s", observed=observed(parties={"payee_id": "other"}), confirmed=True), V.FAIL),
    ],
)
def test_verifier_verdicts(evidence, verdict):
    assert verify_evidence(TERMS, evidence, VerificationSpec()).verdict is verdict


# ---- executor ---------------------------------------------------------------------------------


async def test_recheck_only_advances_when_terms_hold(factory, world):
    async with factory() as s, s.begin():
        ok = await world["executor"].recheck(s, run_id="r1", terms=TERMS)
    assert ok.state is ExecutionState.RECHECKED and ok.report.can_proceed
    world["observer"].current = observed(total_minor=560000)
    async with factory() as s, s.begin():
        blocked = await world["executor"].recheck(s, run_id="r1", terms=TERMS)
    assert blocked.state is ExecutionState.PROPOSED and blocked.report.action is MismatchAction.REAPPROVE


async def test_end_to_end_happy_path_captures_the_verified_amount(factory, world):
    placed = await place(factory, world)
    assert placed.state is ExecutionState.AWAITING_CONFIRMATION
    pid = placed.created.payment.id
    world["provider"].order_status = "payer_approved"
    world["source"].evidence = Evidence(source="shop", observed=observed(total_minor=510000), confirmed=True)
    result = await settle(factory, world, pid)
    assert result.state is ExecutionState.SETTLED and result.captured == Decimal(5100)
    assert await ledger(factory, pid) == "captured"


async def test_price_rise_blocks_order_before_any_provider_call(factory, world):
    world["observer"].current = observed(total_minor=560000)
    with pytest.raises(ExecutionBlocked) as info:
        await place(factory, world)
    assert info.value.report.action is MismatchAction.REAPPROVE
    assert not world["provider"].calls


async def test_unverified_approval_cannot_place_an_order(factory, world):
    forged = world["approved"][Action.CREATE_ORDER].model_copy(update={"signature": "0" * 64})
    permit = PaymentPermit(AUTH, "shop", (forged,), world["permit"].verifier)
    with pytest.raises(ExecutionBlocked):
        async with factory() as s, s.begin():
            await world["executor"].place_order(s, permit=permit, approval=forged, terms=TERMS, order_id="o1", payee_id="m1")
    assert not world["provider"].calls


async def test_settle_waits_for_the_payer(factory, world):
    pid = (await place(factory, world)).created.payment.id
    with pytest.raises(PaymentNotReady):
        await settle(factory, world, pid)
    assert await ledger(factory, pid) == "created"


async def test_terms_changing_before_settlement_voids_without_holding_funds(factory, world):
    pid = (await place(factory, world)).created.payment.id
    world["provider"].order_status = "payer_approved"
    world["observer"].current = observed(available=False)
    result = await settle(factory, world, pid)
    assert result.state is ExecutionState.VOIDED and result.report.action is MismatchAction.UNAVAILABLE
    assert await ledger(factory, pid) == "voided"
    assert not [c for c in world["provider"].calls if c[0] in ("authorize", "capture", "void")]


async def test_failed_verification_voids_the_hold(factory, world):
    pid = (await place(factory, world)).created.payment.id
    world["provider"].order_status = "payer_approved"
    world["source"].evidence = Evidence(source="shop", observed=observed(subject_attributes={"size": "L"}), confirmed=True)
    result = await settle(factory, world, pid)
    assert result.state is ExecutionState.VOIDED and result.verification.verdict is V.FAIL
    assert await ledger(factory, pid) == "voided"
    assert [c[0] for c in world["provider"].calls].count("void") == 1


async def test_pending_evidence_keeps_the_hold_then_voids_after_max_attempts(factory, world):
    pid = (await place(factory, world)).created.payment.id
    world["provider"].order_status = "payer_approved"
    world["source"].evidence = Evidence(source="shop", observed=observed(), confirmed=None)
    first = await settle(factory, world, pid, attempt=1)
    assert first.state is ExecutionState.AWAITING_CONFIRMATION and await ledger(factory, pid) == "authorized"
    last = await settle(factory, world, pid, attempt=3)
    assert last.state is ExecutionState.EXPIRED and await ledger(factory, pid) == "voided"


async def test_late_confirmation_after_waiting_captures(factory, world):
    pid = (await place(factory, world)).created.payment.id
    world["provider"].order_status = "payer_approved"
    world["source"].evidence = Evidence(source="shop", observed=observed(), confirmed=None)
    await settle(factory, world, pid, attempt=1)
    world["source"].evidence = Evidence(source="shop", observed=observed(), confirmed=True)
    result = await settle(factory, world, pid, attempt=2)
    assert result.state is ExecutionState.SETTLED and await ledger(factory, pid) == "captured"


async def test_every_executor_decision_is_audited(factory, world):
    pid = (await place(factory, world)).created.payment.id
    world["provider"].order_status = "payer_approved"
    await settle(factory, world, pid)
    async with factory() as s:
        actions = (await s.execute(select(__import__("app.db.models", fromlist=["AuditLog"]).AuditLog.action))).scalars().all()
    for expected in ("EXECUTION_PRE_ORDER_RECHECK", "EXECUTION_PRE_SETTLE_RECHECK", "EXECUTION_VERIFY", "PAYMENT_CAPTURED"):
        assert expected in actions


def test_provider_order_type_still_exported():
    assert ProviderOrder.model_fields["payee_id"]
