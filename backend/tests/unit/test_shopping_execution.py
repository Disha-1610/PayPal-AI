"""Shopping terms, connector-backed recheck and Monitor-event evidence, driven through the real Executor rules."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.base import ConnectorError
from app.connectors.base.models import StockResponse
from app.db.base import Base
from app.db.models.monitor import MonitorEventRecord
from app.domains.shopping_execution import ConnectorTermsObserver, MonitorEventEvidence, ShoppingTerms
from app.orchestrator.agents.verifier import verify_evidence
from app.orchestrator.executor.mismatch import classify_mismatch
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, Operator
from app.schemas.recheck import MismatchAction
from app.schemas.verification import VerificationSpec, VerificationVerdict

NOW = datetime(2026, 11, 11, tzinfo=UTC)
ORDER = "order-r1-abc"


def candidate(total="49.99", currency="USD", **kw):
    return Candidate(
        candidate_id="sku-1", source="channel3", domain="shopping", title="Trail shoe",
        total_cost=None if total is None else Decimal(total), currency=currency, **kw,
    )


def state(days=None):
    constraints = []
    if days is not None:
        constraints.append(
            Constraint(field_name="delivery_days", operator=Operator.LTE, target_value=days, source=ConstraintSource.USER)
        )
    return {"run_id": "r1", "constraints": constraints}


def terms_for(days=None):
    return ShoppingTerms("MERCHANT1", clock=lambda: NOW)(candidate(), state(days))


class FakeRegistry:
    def __init__(self, cand=None, stock=StockResponse(available=True), error=None):
        self.cand, self.stock_response, self.error, self.auths = cand or candidate(), stock, error, []

    async def details(self, auth, connector_id, request):
        self.auths.append((auth, connector_id))
        if self.error:
            raise self.error
        return self.cand

    async def stock(self, auth, connector_id, request):
        if self.error:
            raise self.error
        return self.stock_response


# ---- terms -----------------------------------------------------------------------------------------------

def test_terms_pin_item_price_payee_deadline_and_run():
    terms = terms_for(days=5)
    assert terms.subject_id == "sku-1" and terms.max_total_minor == 4999 == terms.expected_total_minor
    assert terms.parties == {"payee_id": "MERCHANT1"} and terms.latest_acceptable_at == NOW + timedelta(days=5)
    assert terms.domain_data == {"platform": "channel3", "run_id": "r1"}


def test_no_delivery_constraint_means_no_deadline():
    assert terms_for().latest_acceptable_at is None


@pytest.mark.parametrize("cand", [candidate(total=None), candidate(currency=None)])
def test_a_candidate_without_a_known_price_cannot_be_approved(cand):
    with pytest.raises(ValueError, match="no known total cost"):
        ShoppingTerms("M")(cand, state())


def test_payee_is_required():
    with pytest.raises(ValueError):
        ShoppingTerms("")


# ---- observer --------------------------------------------------------------------------------------------

async def test_unchanged_listing_proceeds_and_authority_is_search_only_on_the_approved_platform():
    terms, registry = terms_for(), FakeRegistry()
    observed = await ConnectorTermsObserver(registry, clock=lambda: NOW).observe(terms)
    assert classify_mismatch(terms, observed).action is MismatchAction.PROCEED
    auth, platform = registry.auths[0]
    assert platform == "channel3" and auth.permitted_platforms == {"channel3"} and len(auth.permitted_actions) == 1
    assert auth.run_id == "r1"


async def test_price_increase_requires_reapproval():
    terms = terms_for()
    observed = await ConnectorTermsObserver(FakeRegistry(candidate("59.99")), clock=lambda: NOW).observe(terms)
    assert classify_mismatch(terms, observed).action is MismatchAction.REAPPROVE


async def test_out_of_stock_is_unavailable_and_low_stock_too():
    terms = terms_for()
    for stock in (StockResponse(available=False), StockResponse(available=True, quantity=0)):
        observed = await ConnectorTermsObserver(FakeRegistry(stock=stock), clock=lambda: NOW).observe(terms)
        assert classify_mismatch(terms, observed).action is MismatchAction.UNAVAILABLE


async def test_connector_failure_is_unknown_never_a_pass():
    terms = terms_for()
    observer = ConnectorTermsObserver(FakeRegistry(error=ConnectorError("down")), clock=lambda: NOW)
    assert classify_mismatch(terms, await observer.observe(terms)).action is MismatchAction.INSUFFICIENT_DATA


async def test_unknown_delivery_estimate_with_a_deadline_blocks_instead_of_guessing():
    terms = terms_for(days=5)  # candidate has no available_by
    observed = await ConnectorTermsObserver(FakeRegistry(), clock=lambda: NOW).observe(terms)
    assert classify_mismatch(terms, observed).action is MismatchAction.INSUFFICIENT_DATA


async def test_a_different_currency_is_not_comparable():
    terms = terms_for()
    observed = await ConnectorTermsObserver(FakeRegistry(candidate("49.99", "EUR")), clock=lambda: NOW).observe(terms)
    assert classify_mismatch(terms, observed).action is MismatchAction.INSUFFICIENT_DATA


# ---- evidence --------------------------------------------------------------------------------------------

@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def add_event(factory, event_type, at, *, status="applied", entity=ORDER, event_id=None):
    async with factory() as session, session.begin():
        session.add(
            MonitorEventRecord(
                event_id=event_id or f"{event_type}-{at.isoformat()}", source="seller", run_id="r1", domain="shopping",
                event_type=event_type, entity_id=entity, occurred_at=at, status=status,
            )
        )


def evidence_source(factory, cand=None):
    return MonitorEventEvidence(ConnectorTermsObserver(FakeRegistry(cand), clock=lambda: NOW), factory)


SPEC = VerificationSpec()


async def verdict(factory, terms, cand=None):
    evidence = await evidence_source(factory, cand).collect(terms, ORDER)
    return verify_evidence(terms, evidence, SPEC).verdict


async def test_no_events_yet_is_insufficient_evidence_so_nothing_is_captured(factory):
    assert await verdict(factory, terms_for()) is VerificationVerdict.INSUFFICIENT_EVIDENCE


async def test_shipped_is_not_delivery(factory):
    await add_event(factory, "SHIPPED", NOW + timedelta(days=1))
    assert await verdict(factory, terms_for()) is VerificationVerdict.INSUFFICIENT_EVIDENCE


async def test_delivery_on_time_passes(factory):
    await add_event(factory, "SHIPPED", NOW + timedelta(days=1))
    await add_event(factory, "DELIVERED", NOW + timedelta(days=3))
    assert await verdict(factory, terms_for(days=5)) is VerificationVerdict.PASS


async def test_late_delivery_fails_the_deadline(factory):
    await add_event(factory, "DELIVERED", NOW + timedelta(days=9))
    assert await verdict(factory, terms_for(days=5)) is VerificationVerdict.FAIL


@pytest.mark.parametrize("event_type", ["DELIVERY_FAILED", "RETURNED"])
async def test_failed_delivery_or_return_fails(factory, event_type):
    await add_event(factory, event_type, NOW + timedelta(days=2))
    assert await verdict(factory, terms_for()) is VerificationVerdict.FAIL


async def test_rejected_stale_and_other_orders_events_are_ignored(factory):
    await add_event(factory, "DELIVERED", NOW + timedelta(days=1), status="rejected", event_id="a")
    await add_event(factory, "DELIVERED", NOW + timedelta(days=1), entity="other-order", event_id="b")
    assert await verdict(factory, terms_for()) is VerificationVerdict.INSUFFICIENT_EVIDENCE


async def test_delivery_cannot_pass_if_the_live_price_rose(factory):
    await add_event(factory, "DELIVERED", NOW + timedelta(days=1))
    assert await verdict(factory, terms_for(), candidate("59.99")) is VerificationVerdict.FAIL
