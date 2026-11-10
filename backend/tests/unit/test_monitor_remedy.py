"""Phase 7: refund/dispute are permission-gated, deterministic, idempotent and audited."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.payment import Payment, PaymentEvent
from app.monitor.remedy import RemedyOutcome, prepare_dispute, request_refund
from app.payments.service import (
    PaymentNotReady,
    PaymentPermit,
    PaymentServiceError,
)
from app.schemas.authorization import Action, TaskAuthorization
from tests.unit.payment_fakes import FakeProvider
from tests.unit.test_payment_service import (
    ALL_MONEY,
    AUTH,
    FULL,
    VERIFIER,
    authorized_payment,
    signed,
)

REFUND_ACTIONS = frozenset({Action.REQUEST_REFUND, Action.EXECUTE_REFUND, Action.OPEN_DISPUTE})


def grant(*, permitted: frozenset[Action] = REFUND_ACTIONS, approval_required: frozenset[Action] = frozenset()):
    return TaskAuthorization(
        **{
            **AUTH.model_dump(),
            "permitted_actions": AUTH.permitted_actions | permitted,
            "approval_required_actions": frozenset(ALL_MONEY) | approval_required,
        }
    )


def permit_for(auth: TaskAuthorization, *approved: Action) -> PaymentPermit:
    return PaymentPermit(auth, "shop", tuple(signed(a) for a in approved), VERIFIER)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def captured(factory, provider=None):
    provider = provider or FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL)
    return service, pid, provider


async def refund(factory, service, pid, permit):
    async with factory() as s, s.begin():
        return await request_refund(s, service, pid, permit=permit, reason="not delivered")


async def status(factory, pid):
    async with factory() as s:
        return (await s.get(Payment, pid)).status


async def test_permitted_refund_executes_and_is_ledgered_and_audited(factory):
    service, pid, provider = await captured(factory)
    decision = await refund(factory, service, pid, permit_for(grant()))
    assert decision.outcome is RemedyOutcome.EXECUTED and await status(factory, pid) == "refunded"
    assert [c for c in provider.calls if c[0] == "refund"] == [("refund", f"{pid}:refund")]
    async with factory() as s:
        events = (await s.execute(select(PaymentEvent.event_type).where(PaymentEvent.payment_id == pid))).scalars().all()
        audit = (await s.execute(text("select action from audit_logs"))).scalars().all()
    assert "refunded" in events and {"PAYMENT_REFUNDED", "REMEDY_EXECUTE_REFUND"} <= set(audit)


async def test_refund_is_denied_by_default(factory):
    service, pid, provider = await captured(factory)
    decision = await refund(factory, service, pid, permit_for(grant(permitted=frozenset())))
    assert decision.outcome is RemedyOutcome.DENIED and "not permitted" in decision.reason
    assert await status(factory, pid) == "captured" and not any(c[0] == "refund" for c in provider.calls)


async def test_request_permission_alone_does_not_allow_execution(factory):
    service, pid, provider = await captured(factory)
    decision = await refund(factory, service, pid, permit_for(grant(permitted=frozenset({Action.REQUEST_REFUND}))))
    assert decision.outcome is RemedyOutcome.DENIED and await status(factory, pid) == "captured"
    assert not any(c[0] == "refund" for c in provider.calls)


async def test_approval_required_refund_waits_for_a_signed_approval(factory):
    service, pid, provider = await captured(factory)
    auth = grant(approval_required=frozenset({Action.EXECUTE_REFUND}))
    waiting = await refund(factory, service, pid, permit_for(auth))
    assert waiting.outcome is RemedyOutcome.NEEDS_APPROVAL and await status(factory, pid) == "captured"
    forged = permit_for(auth, Action.EXECUTE_REFUND)
    forged = PaymentPermit(auth, "shop", (forged.approvals[0].model_copy(update={"signature": "0" * 64}),), VERIFIER)
    assert (await refund(factory, service, pid, forged)).outcome is RemedyOutcome.NEEDS_APPROVAL
    assert not any(c[0] == "refund" for c in provider.calls)
    done = await refund(factory, service, pid, permit_for(auth, Action.EXECUTE_REFUND))
    assert done.outcome is RemedyOutcome.EXECUTED and await status(factory, pid) == "refunded"


async def test_refund_cannot_touch_another_runs_payment(factory):
    service, pid, _ = await captured(factory)
    other = TaskAuthorization(**{**grant().model_dump(), "run_id": "r2", "authorization_id": "a2"})
    decision = await refund(factory, service, pid, permit_for(other))
    assert decision.outcome is RemedyOutcome.DENIED and await status(factory, pid) == "captured"


async def test_refund_replay_is_idempotent(factory):
    service, pid, provider = await captured(factory)
    for _ in range(2):
        assert (await refund(factory, service, pid, permit_for(grant()))).outcome is RemedyOutcome.EXECUTED
    assert len([c for c in provider.calls if c[0] == "refund"]) == 1


async def test_refund_needs_a_captured_payment(factory):
    service, pid = await authorized_payment(factory, FakeProvider())
    with pytest.raises(PaymentServiceError, match="cannot refund"):
        async with factory() as s, s.begin():
            await service.refund(s, pid, permit=permit_for(grant()), reason="x")


async def test_pending_refund_leaves_ledger_captured(factory):
    provider = FakeProvider()
    service, pid, _ = await captured(factory, provider)
    provider.refund_status = "pending"
    with pytest.raises(PaymentNotReady):
        await refund(factory, service, pid, permit_for(grant()))
    assert await status(factory, pid) == "captured"


async def test_dispute_is_prepared_only_when_permitted(factory):
    _, pid, _ = await captured(factory)
    async with factory() as s, s.begin():
        denied = await prepare_dispute(s, pid, permit=permit_for(grant(permitted=frozenset())), reason="x")
    async with factory() as s, s.begin():
        ok = await prepare_dispute(s, pid, permit=permit_for(grant()), reason="item not received")
    assert denied.outcome is RemedyOutcome.DENIED and ok.outcome is RemedyOutcome.PREPARED
    async with factory() as s:
        audit = (await s.execute(text("select decision from audit_logs where action='REMEDY_OPEN_DISPUTE'"))).scalars().all()
    assert sorted(audit) == ["allow", "deny"]


async def test_dispute_requiring_approval_asks_the_user(factory):
    _, pid, _ = await captured(factory)
    async with factory() as s, s.begin():
        d = await prepare_dispute(
            s, pid, permit=permit_for(grant(approval_required=frozenset({Action.OPEN_DISPUTE}))), reason="x"
        )
    assert d.outcome is RemedyOutcome.NEEDS_APPROVAL
