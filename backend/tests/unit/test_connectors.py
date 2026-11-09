import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest

from app.connectors.base import Connector, ConnectorError, TextRecord
from app.connectors.base.models import (
    CandidateRequest,
    CheckoutRequest,
    DeliveryRequest,
    Listing,
    ReviewsRequest,
    SearchRequest,
)
from app.connectors.flaky import FlakyConnector
from app.connectors.mocks.amazon import INJECTION_REVIEW, build_mock_amazon
from app.connectors.mocks.contractor import build_mock_contractor
from app.connectors.mocks.flipkart import build_mock_flipkart
from app.connectors.registry import ConnectorRegistry
from app.schemas.authorization import Action, Approval, TaskAuthorization
from app.schemas.candidate import Candidate, DataQuality

READ = frozenset({Action.SEARCH})
ALL = frozenset({Action.SEARCH, Action.CREATE_ORDER})


class MemoryTextStore:
    def __init__(self) -> None:
        self.rows: dict[str, TextRecord] = {}

    async def save(self, *, run_id, source, subject_id, records):
        ids = [f"t{len(self.rows) + i}" for i in range(len(records))]
        self.rows.update(zip(ids, records, strict=True))
        return ids


def make_auth(platforms, actions=ALL, **kw) -> TaskAuthorization:
    return TaskAuthorization(
        run_id="r1", max_spend=Decimal("10000"), permitted_platforms=frozenset(platforms), permitted_actions=actions, **kw
    )


@pytest.fixture
def store():
    return MemoryTextStore()


def make_registry(store, *connectors) -> ConnectorRegistry:
    registry = ConnectorRegistry(store, timeout=1.0)
    for c in connectors or (build_mock_amazon(), build_mock_flipkart(), build_mock_contractor()):
        registry.register(c)
    return registry


SHOES = SearchRequest(query="running shoes", domain="shopping")


def test_mocks_satisfy_connector_protocol():
    assert all(isinstance(c, Connector) for c in (build_mock_amazon(), build_mock_flipkart(), build_mock_contractor()))


async def test_search_is_limited_to_permitted_platforms(store):
    registry = make_registry(store)
    result = await registry.search_all(make_auth({"mock_amazon"}), SHOES)
    assert {c.source for c in result.candidates} == {"mock_amazon"}


async def test_failure_of_permitted_platform_never_widens_scope(store):
    broken = FlakyConnector(build_mock_amazon(), fail_capabilities=frozenset({"search"}))
    registry = make_registry(store, broken, build_mock_flipkart())
    result = await registry.search_all(make_auth({"mock_amazon"}), SHOES)
    assert result.candidates == ()
    assert len(result.errors) == 1 and "mock_amazon" in result.errors[0]


async def test_direct_call_to_unpermitted_platform_is_denied(store):
    registry = make_registry(store)
    with pytest.raises(ConnectorError, match="not permitted"):
        await registry.details(make_auth({"mock_amazon"}), "mock_flipkart", CandidateRequest(candidate_id="fk-1"))


async def test_one_flaky_platform_does_not_block_the_other(store):
    broken = FlakyConnector(build_mock_amazon(), fail_capabilities=frozenset({"search"}))
    registry = make_registry(store, broken, build_mock_flipkart())
    result = await registry.search_all(make_auth({"mock_amazon", "mock_flipkart"}), SHOES)
    assert {c.source for c in result.candidates} == {"mock_flipkart"}
    assert len(result.errors) == 1


async def test_timeout_becomes_connector_error(store):
    class Hanging:  # conftest patches asyncio.sleep globally, so block on an event instead
        id, domain = "mock_amazon", "shopping"
        capabilities = build_mock_amazon().capabilities

        async def execute(self, capability, request):
            await asyncio.Event().wait()

    registry = make_registry(store, Hanging())
    registry._timeout = 0.05
    result = await registry.search_all(make_auth({"mock_amazon"}), SHOES)
    assert result.candidates == () and "timed out" in result.errors[0]


async def test_unsupported_capability_is_refused(store):
    registry = make_registry(store)
    auth = make_auth({"mock_flipkart"})
    with pytest.raises(ConnectorError, match="does not support 'stock'"):
        await registry.stock(auth, "mock_flipkart", CandidateRequest(candidate_id="fk-1"))
    with pytest.raises(ConnectorError, match="does not support 'delivery'"):
        await registry.delivery(auth, "mock_flipkart", DeliveryRequest(candidate_id="fk-1", destination="560001"))


async def test_checkout_needs_create_order_permission_and_budget(store):
    registry = make_registry(store)
    request = CheckoutRequest(candidate_id="amz-1", amount=Decimal("4499"), currency="INR")
    with pytest.raises(ConnectorError, match="action create_order not permitted"):
        await registry.checkout(make_auth({"mock_amazon"}, actions=READ), "mock_amazon", request, [])
    over = request.model_copy(update={"amount": Decimal("99999")})
    with pytest.raises(ConnectorError, match="max_spend"):
        await registry.checkout(make_auth({"mock_amazon"}), "mock_amazon", over, [])
    done = await registry.checkout(make_auth({"mock_amazon"}), "mock_amazon", request, [])
    assert done.status == "created"


async def test_checkout_honours_required_approval(store):
    registry = make_registry(store)
    auth = make_auth({"mock_amazon"}, approval_required_actions=frozenset({Action.CREATE_ORDER}))
    request = CheckoutRequest(candidate_id="amz-1", amount=Decimal("4499"), currency="INR")
    with pytest.raises(ConnectorError, match="approval required"):
        await registry.checkout(auth, "mock_amazon", request, [])
    approval = Approval(
        run_id="r1", authorization_id=auth.authorization_id, action=Action.CREATE_ORDER, candidate_id="amz-1",
        platform="mock_amazon", amount=Decimal("4499"), currency="INR", approved_by="user",
    )
    assert (await registry.checkout(auth, "mock_amazon", request, [approval])).order_ref


async def test_same_candidate_shape_across_domains(store):
    registry = make_registry(store)
    auth = make_auth({"mock_amazon", "mock_flipkart", "mock_contractors"})
    shopping = await registry.search_all(auth, SHOES)
    work = await registry.search_all(auth, SearchRequest(query="web developer", domain="contractor"))
    assert shopping.candidates and work.candidates
    assert all(isinstance(c, Candidate) for c in (*shopping.candidates, *work.candidates))
    assert {c.domain for c in work.candidates} == {"contractor"}


async def test_data_quality_flags(store):
    registry = make_registry(store)
    result = await registry.search_all(make_auth({"mock_amazon", "mock_flipkart"}), SHOES)
    by_id = {c.candidate_id: c for c in result.candidates}
    assert by_id["amz-2"].quality["rating"] is DataQuality.UNRELIABLE
    assert by_id["fk-1"].quality["available_by"] is DataQuality.MISSING
    assert by_id["amz-1"].quality == {}


async def test_stale_data_is_flagged(store):
    stale = FlakyConnector(build_mock_amazon(), stale_by=timedelta(days=3))
    registry = make_registry(store, stale)
    result = await registry.search_all(make_auth({"mock_amazon"}), SHOES)
    assert result.candidates[0].quality["rating"] is DataQuality.STALE


async def test_untrusted_text_is_stored_and_only_ids_returned(store):
    registry = make_registry(store)
    auth = make_auth({"mock_amazon"})
    summary = await registry.reviews(auth, "mock_amazon", ReviewsRequest(candidate_id="amz-1"))
    found = await registry.search_all(auth, SHOES)
    bodies = {r.body for r in store.rows.values()}
    assert INJECTION_REVIEW in bodies
    assert set(summary.text_refs) <= set(store.rows)
    assert summary.review_count == 2 and summary.average_rating == 3.0
    assert INJECTION_REVIEW not in summary.model_dump_json()
    assert INJECTION_REVIEW not in found.model_dump_json()
    assert all("Lightweight running shoes" not in c.model_dump_json() for c in found.candidates)
    assert any(c.text_refs for c in found.candidates)


async def test_invalid_attribute_rejects_only_that_candidate(store):
    class BadConnector:
        id, domain = "bad", "shopping"
        capabilities = build_mock_amazon().capabilities

        async def execute(self, capability, request):
            from app.connectors.base.models import SearchResponse

            ok = Candidate(candidate_id="b1", source="bad", domain="shopping", title="ok", attributes={"brand": "x"})
            bad = Candidate(candidate_id="b2", source="bad", domain="shopping", title="bad", attributes={"warp": 9})
            typed = Candidate(candidate_id="b3", source="bad", domain="shopping", title="typed", attributes={"brand": 5})
            return SearchResponse(listings=tuple(Listing(candidate=c) for c in (ok, bad, typed)))

    registry = make_registry(store, BadConnector())
    result = await registry.search_all(make_auth({"bad"}), SHOES)
    assert [c.candidate_id for c in result.candidates] == ["b1"]
    assert len(result.errors) == 2


async def test_candidate_source_must_match_connector(store):
    class Spoofer:
        id, domain = "spoof", "shopping"
        capabilities = build_mock_amazon().capabilities

        async def execute(self, capability, request):
            from app.connectors.base.models import SearchResponse

            other = Candidate(candidate_id="x", source="mock_amazon", domain="shopping", title="x")
            return SearchResponse(listings=(Listing(candidate=other),))

    registry = make_registry(store, Spoofer())
    result = await registry.search_all(make_auth({"spoof"}), SHOES)
    assert result.candidates == () and "claims source" in result.errors[0]


def test_duplicate_registration_rejected(store):
    registry = make_registry(store, build_mock_amazon())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(build_mock_amazon())
