from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models.payment import Payment, PaymentEvent, PaymentStatus
from app.orchestrator.guard.approval_guard import (
    issue_approval,
    signed_approval_verifier,
)
from app.payments.provider import ProviderError
from app.payments.service import (
    PaymentMismatch,
    PaymentNotAuthorized,
    PaymentNotReady,
    PaymentPermit,
    PaymentService,
    PaymentServiceError,
)
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, Approval, TaskAuthorization
from tests.unit.payment_fakes import FakeProvider

KEY = b"k" * 32
KEYS = {"v1": KEY}
ALL_MONEY = (Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT)
TERMS = ApprovalTerms(subject_id="shoe-1", parties={"payee_id": "m1"}, currency="INR", max_total_minor=550000)
VERIFIER = signed_approval_verifier(current_terms=TERMS, keys=KEYS, run_id="r1", session_id="s1", approved_by="u1")
AUTH = TaskAuthorization(
    authorization_id="a1", run_id="r1", max_spend=Decimal(6000), permitted_platforms=frozenset({"shop"}),
    permitted_actions=frozenset({*ALL_MONEY, Action.VOID_PAYMENT}),
)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def signed(action: Action, **kw) -> Approval:
    args = {
        "terms": TERMS, "run_id": "r1", "authorization_id": "a1", "action": action, "candidate_id": "shoe-1",
        "platform": "shop", "approved_by": "u1", "session_id": "s1", "key": KEY, "key_id": "v1", "ttl": timedelta(minutes=10),
    }
    return issue_approval(**{**args, **kw})


def permit(*actions: Action, auth: TaskAuthorization = AUTH) -> tuple[PaymentPermit, dict[Action, Approval]]:
    approvals = {action: signed(action) for action in actions}
    return PaymentPermit(auth, "shop", tuple(approvals.values()), VERIFIER), approvals


FULL, FULL_APPROVALS = permit(*ALL_MONEY)


async def make_payment(factory, provider, amount="5200"):
    service = PaymentService(provider)
    async with factory() as s, s.begin():
        created = await service.create(
            s, permit=FULL, approval=FULL_APPROVALS[Action.CREATE_ORDER], order_id="o1", amount=Decimal(amount), payee_id="m1"
        )
        payment_id = created.payment.id
    return service, payment_id


async def status(factory, payment_id):
    async with factory() as s:
        return (await s.get(Payment, payment_id)).status


async def events(factory, payment_id):
    async with factory() as s:
        rows = (await s.execute(select(PaymentEvent.event_type).where(PaymentEvent.payment_id == payment_id))).scalars().all()
    return sorted(rows)


async def authorized_payment(factory, provider):
    service, pid = await make_payment(factory, provider)
    provider.order_status = "payer_approved"
    async with factory() as s, s.begin():
        await service.authorize(s, pid, permit=FULL)
    return service, pid


async def test_happy_path_create_authorize_capture(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    assert await status(factory, pid) == "authorized"
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL)
    assert await status(factory, pid) == "captured"
    assert await events(factory, pid) == ["authorized", "captured", "order_created"]
    async with factory() as s:
        audit = (await s.execute(text("select action from audit_logs order by timestamp"))).scalars().all()
    assert audit == ["PAYMENT_ORDER_CREATED", "PAYMENT_AUTHORIZED", "PAYMENT_CAPTURED"]


async def test_create_is_idempotent_per_approval(factory):
    provider = FakeProvider()
    service = PaymentService(provider)
    ids = []
    for _ in range(2):
        async with factory() as s, s.begin():
            created = await service.create(
                s, permit=FULL, approval=FULL_APPROVALS[Action.CREATE_ORDER], order_id="o1", amount=Decimal(5200), payee_id="m1"
            )
            ids.append(created.payment.id)
    assert ids[0] == ids[1]
    assert [c for c in provider.calls if c[0] == "create"] == [("create", f"{FULL_APPROVALS[Action.CREATE_ORDER].approval_id}:create")]


async def test_same_approval_cannot_be_reused_for_another_amount(factory):
    service, _ = await make_payment(factory, FakeProvider(), amount="5200")
    with pytest.raises(PaymentServiceError, match="different amount"):
        async with factory() as s, s.begin():
            await service.create(
                s, permit=FULL, approval=FULL_APPROVALS[Action.CREATE_ORDER], order_id="o1", amount=Decimal(5400), payee_id="m1"
            )


async def test_create_rejects_unverified_and_out_of_ceiling_approvals(factory):
    service = PaymentService(FakeProvider())
    forged = FULL_APPROVALS[Action.CREATE_ORDER].model_copy(update={"signature": "0" * 64})
    forged_permit = PaymentPermit(AUTH, "shop", (forged,), VERIFIER)
    with pytest.raises(PaymentNotAuthorized):
        async with factory() as s, s.begin():
            await service.create(s, permit=forged_permit, approval=forged, order_id="o1", amount=Decimal(5200), payee_id="m1")
    with pytest.raises(PaymentNotAuthorized, match="approval required"):
        async with factory() as s, s.begin():
            await service.create(
                s, permit=FULL, approval=FULL_APPROVALS[Action.CREATE_ORDER], order_id="o1", amount=Decimal(5501), payee_id="m1"
            )
    with pytest.raises(PaymentNotAuthorized, match="verified create_order"):  # a capture approval cannot create
        async with factory() as s, s.begin():
            await service.create(
                s, permit=FULL, approval=FULL_APPROVALS[Action.CAPTURE_PAYMENT], order_id="o1", amount=Decimal(5200), payee_id="m1"
            )


async def test_authorize_and_capture_need_their_own_verified_approvals(factory):
    provider = FakeProvider()
    create_only, approvals = permit(Action.CREATE_ORDER)
    service = PaymentService(provider)
    async with factory() as s, s.begin():
        created = await service.create(
            s, permit=create_only, approval=approvals[Action.CREATE_ORDER], order_id="o1", amount=Decimal(5200), payee_id="m1"
        )
        pid = created.payment.id
    provider.order_status = "payer_approved"
    with pytest.raises(PaymentNotAuthorized, match="authorize_payment"):
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=create_only)
    assert not [c for c in provider.calls if c[0] == "authorize"]

    with_authorize, _ = permit(Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT)
    async with factory() as s, s.begin():
        await service.authorize(s, pid, permit=with_authorize)
    with pytest.raises(PaymentNotAuthorized, match="capture_payment"):
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=with_authorize)
    assert not [c for c in provider.calls if c[0] == "capture"]
    assert await status(factory, pid) == "authorized"


async def test_payment_must_belong_to_the_permits_run(factory):
    service, pid = await make_payment(factory, FakeProvider())
    other_auth = TaskAuthorization(
        authorization_id="a2", run_id="r2", max_spend=Decimal(6000), permitted_platforms=frozenset({"shop"}),
        permitted_actions=frozenset({*ALL_MONEY, Action.VOID_PAYMENT}),
    )
    other, _ = permit(*ALL_MONEY, auth=other_auth)
    with pytest.raises(PaymentNotAuthorized, match="does not belong"):
        async with factory() as s, s.begin():
            await service.void(s, pid, permit=other, reason="x")


async def test_authorize_blocks_until_payer_approves(factory):
    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    with pytest.raises(PaymentNotReady):
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
    assert await status(factory, pid) == "created"


async def test_authorize_refuses_when_provider_order_differs(factory):
    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    provider.order_status = "payer_approved"
    provider.order_amount = Decimal(5800)
    with pytest.raises(PaymentMismatch, match="approved"):
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
    provider.order_amount, provider.order_payee = None, "attacker"
    with pytest.raises(PaymentMismatch, match="payee"):
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
    assert await status(factory, pid) == "created"
    assert not [c for c in provider.calls if c[0] == "authorize"]


async def test_missing_payee_at_the_provider_fails_closed(factory):
    provider = FakeProvider()
    provider.omit_payee = True
    with pytest.raises(PaymentMismatch, match="missing"):
        await make_payment(factory, provider)

    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    provider.order_status, provider.omit_payee = "payer_approved", True
    with pytest.raises(PaymentMismatch, match="missing"):
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
    assert await status(factory, pid) == "created"


async def test_partial_capture_and_over_capture_guard(factory):
    service, pid = await authorized_payment(factory, FakeProvider())
    with pytest.raises(PaymentServiceError, match="within the authorized"):
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL, amount=Decimal(5201))
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL, amount=Decimal(5000))
    async with factory() as s:
        event = (await s.execute(select(PaymentEvent).where(PaymentEvent.event_type == "captured"))).scalar_one()
    assert event.payload == {"captured_minor": 500000, "authorized_minor": 520000}


async def test_pending_capture_leaves_ledger_authorized(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.capture_status = "pending"
    with pytest.raises(PaymentNotReady):
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL)
    assert await status(factory, pid) == "authorized"


async def test_void_before_authorization_needs_no_approval_and_holds_nothing(factory):
    no_approvals, _ = permit()  # releasing funds is always allowed for the owning run
    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    async with factory() as s, s.begin():
        await service.void(s, pid, permit=no_approvals, reason="user cancelled")
    assert await status(factory, pid) == "voided"
    assert not [c for c in provider.calls if c[0] == "void"]  # nothing was held


async def test_void_after_authorization_releases_the_hold(factory):
    no_approvals, _ = permit()
    provider2 = FakeProvider()
    service, pid = await authorized_payment(factory, provider2)
    async with factory() as s, s.begin():
        await service.void(s, pid, permit=no_approvals, reason="verification failed")
    assert await status(factory, pid) == PaymentStatus.VOIDED.value
    assert [c[0] for c in provider2.calls].count("void") == 1


async def test_terminal_states_cannot_change(factory):
    service, pid = await make_payment(factory, FakeProvider())
    async with factory() as s, s.begin():
        await service.void(s, pid, permit=FULL, reason="x")
    with pytest.raises(PaymentServiceError):
        async with factory() as s, s.begin():
            await service.void(s, pid, permit=FULL, reason="again")
    with pytest.raises(PaymentServiceError, match="expected authorized"):
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL)


async def test_uncertain_provider_failure_changes_nothing_and_is_retryable(factory):
    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    provider.order_status = "payer_approved"
    provider.fail_next = ProviderError("timeout", uncertain=True)
    with pytest.raises(ProviderError) as info:
        async with factory() as s, s.begin():
            await service.authorize(s, pid, permit=FULL)
    assert info.value.uncertain
    assert await status(factory, pid) == "created"
    async with factory() as s, s.begin():
        await service.authorize(s, pid, permit=FULL)  # same idempotency key; provider returns the original result
    assert await status(factory, pid) == "authorized"


async def test_authorize_is_idempotent_once_authorized(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    async with factory() as s, s.begin():
        again = await service.authorize(s, pid, permit=FULL)
    assert again.status == "authorized"
    assert [c[0] for c in provider.calls].count("authorize") == 1


async def test_capture_timeout_after_provider_applied_it_is_recovered_from_paypal(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.fail_next = ProviderError("timeout", uncertain=True)
    provider.auth_status = "captured"  # PayPal applied the capture; only the response was lost
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL)
    assert await status(factory, pid) == "captured"
    async with factory() as s:
        event = (await s.execute(select(PaymentEvent).where(PaymentEvent.event_type == "captured"))).scalar_one()
    assert event.payload["recovered_from_provider"] is True


async def test_capture_timeout_not_applied_leaves_ledger_authorized_and_is_retryable(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.fail_next = ProviderError("timeout", uncertain=True)
    with pytest.raises(ProviderError):
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL)
    assert await status(factory, pid) == "authorized"
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL)
    assert await status(factory, pid) == "captured"


async def test_capture_timeout_with_unreadable_provider_state_stays_uncertain(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.fail_next = ProviderError("timeout", uncertain=True)
    provider.get_authorization_fails = True
    with pytest.raises(ProviderError) as info:
        async with factory() as s, s.begin():
            await service.capture(s, pid, permit=FULL)
    assert info.value.uncertain
    assert await status(factory, pid) == "authorized"


async def test_void_timeout_after_provider_released_the_hold_is_recovered(factory):
    no_approvals, _ = permit()
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.fail_next = ProviderError("timeout", uncertain=True)
    provider.auth_status = "voided"
    async with factory() as s, s.begin():
        await service.void(s, pid, permit=no_approvals, reason="verification failed")
    assert await status(factory, pid) == "voided"


async def test_void_timeout_while_hold_still_active_is_not_recorded(factory):
    no_approvals, _ = permit()
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.fail_next = ProviderError("timeout", uncertain=True)
    with pytest.raises(ProviderError):
        async with factory() as s, s.begin():
            await service.void(s, pid, permit=no_approvals, reason="x")
    assert await status(factory, pid) == "authorized"
