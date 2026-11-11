"""Payment reconciliation: ledger vs provider, append-only records, one safe repair, nothing moved automatically."""

import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models.payment import Payment, PaymentReconciliation
from app.payments.reconciliation import Reconciler
from app.payments.service import PaymentService
from app.workers.reconciler import run_reconciler
from tests.unit.payment_fakes import FakeProvider
from tests.unit.test_payment_service import (  # noqa: F401  (factory is a fixture)
    FULL,
    authorized_payment,
    factory,
    make_payment,
    status,
)


async def rows(factory):
    async with factory() as s:
        return list((await s.execute(select(PaymentReconciliation).order_by(PaymentReconciliation.checked_at))).scalars())


def reconciler(provider, service=None):
    return Reconciler(provider, service or PaymentService(provider), min_interval=timedelta(minutes=5))


async def test_an_open_order_and_an_open_hold_that_agree_are_recorded_as_matched(factory):
    provider = FakeProvider()
    _, created_id = await make_payment(factory, provider)
    summary = await reconciler(provider).sweep(factory)
    assert (summary.checked, summary.matched, summary.mismatched) == (1, 1, 0)
    got = await rows(factory)
    assert got[0].payment_id == created_id and got[0].matched and got[0].ledger_status == "created"
    assert await status(factory, created_id) == "created"


async def test_authorized_with_an_open_hold_matches_and_leaves_the_ledger_alone(factory):
    provider = FakeProvider()
    _, pid = await authorized_payment(factory, provider)
    assert (await reconciler(provider).sweep(factory)).matched == 1
    (row,) = await rows(factory)
    assert (row.ledger_status, row.provider_status, row.matched, row.detail["source"]) == ("authorized", "created", True, "authorization")
    assert await status(factory, pid) == "authorized"


@pytest.mark.parametrize("released", ["voided", "expired"])
async def test_a_hold_released_at_paypal_fails_the_ledger_and_is_recorded(factory, released):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.auth_status = released
    summary = await reconciler(provider, service).sweep(factory)
    assert (summary.repaired, summary.mismatched) == (1, 0)
    assert await status(factory, pid) == "failed"  # ledger only: the run can re-source
    (row,) = await rows(factory)
    assert row.matched is False and row.ledger_status == "authorized" and row.provider_status == released
    assert "repair" in row.detail


async def test_money_disagreements_are_recorded_and_logged_but_never_auto_fixed(factory, caplog):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    provider.auth_status = "captured"  # PayPal captured funds the ledger does not know about
    with caplog.at_level(logging.ERROR):
        summary = await reconciler(provider, service).sweep(factory)
    assert (summary.mismatched, summary.repaired) == (1, 0)
    assert await status(factory, pid) == "authorized"
    assert "disagrees with the provider" in caplog.text
    (row,) = await rows(factory)
    assert row.matched is False and row.provider_status == "captured"


async def test_captured_matches_a_captured_authorization_and_flags_an_uncaptured_one(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    async with factory() as s, s.begin():
        await service.capture(s, pid, permit=FULL)
    provider.auth_status = "captured"
    assert (await reconciler(provider, service).sweep(factory)).matched == 1
    provider.auth_status = "created"  # ledger says captured, PayPal still holds the funds uncaptured
    later = utcnow() + timedelta(minutes=10)
    assert (await reconciler(provider, service).sweep(factory, now=later)).mismatched == 1


async def test_a_voided_payment_must_have_its_hold_gone(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    async with factory() as s, s.begin():
        await service.void(s, pid, permit=FULL, reason="test")
    provider.auth_status = "voided"
    assert (await reconciler(provider, service).sweep(factory)).matched == 1
    provider.auth_status = "created"  # PayPal still holds money for a payment we consider released
    assert (await reconciler(provider, service).sweep(factory, now=utcnow() + timedelta(minutes=10))).mismatched == 1


async def test_voided_before_any_hold_matches_while_the_order_is_unauthorized(factory):
    provider = FakeProvider()
    service, pid = await make_payment(factory, provider)
    async with factory() as s, s.begin():
        await service.void(s, pid, permit=FULL, reason="user cancelled")
    assert (await reconciler(provider, service).sweep(factory)).matched == 1


async def test_an_unreachable_provider_is_skipped_not_reported_as_a_mismatch(factory):
    provider = FakeProvider()
    _, pid = await authorized_payment(factory, provider)
    provider.get_authorization_fails = True
    summary = await reconciler(provider).sweep(factory)
    assert (summary.checked, summary.skipped, summary.mismatched) == (0, 1, 0) and await rows(factory) == []
    provider.get_authorization_fails = False
    assert (await reconciler(provider).sweep(factory)).matched == 1  # checked again on the next pass


async def test_recently_checked_payments_are_not_polled_again_and_records_only_grow(factory):
    provider = FakeProvider()
    await authorized_payment(factory, provider)
    recon = reconciler(provider)
    assert (await recon.sweep(factory)).checked == 1
    assert (await recon.sweep(factory)).checked == 0  # inside min_interval
    assert (await recon.sweep(factory, now=utcnow() + timedelta(minutes=10))).checked == 1
    assert len(await rows(factory)) == 2  # append-only history


async def test_finished_payments_are_not_polled(factory):
    provider = FakeProvider()
    service, pid = await authorized_payment(factory, provider)
    async with factory() as s, s.begin():
        (await s.get(Payment, pid)).status = "refunded"
    assert (await reconciler(provider, service).sweep(factory)).checked == 0


async def test_the_loop_survives_a_failing_pass_and_stops_on_request(factory):
    second_pass = asyncio.Event()

    class Broken:
        calls = 0

        async def sweep(self, _factory):
            Broken.calls += 1
            if Broken.calls >= 2:
                second_pass.set()
            raise RuntimeError("boom")

    stop = asyncio.Event()
    task = asyncio.create_task(run_reconciler(Broken(), factory, interval_seconds=0.01, stop=stop))
    await asyncio.wait_for(second_pass.wait(), 2)  # the first failure did not end the loop
    stop.set()
    await asyncio.wait_for(task, 2)
    assert Broken.calls >= 2
