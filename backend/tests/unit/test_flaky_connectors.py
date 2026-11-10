"""Flaky platforms: empty answers, repeated failures, partial data, timeouts and recovery on retry.

Expected: retries touch only the connectors whose call failed, earlier results are kept, the scope never
widens beyond the permitted platforms, and the attempt limit ends the retrying.
"""

import asyncio
from decimal import Decimal

import pytest

from app.audit.events import AuditScope
from app.connectors.flaky import FlakyConnector
from app.connectors.mocks.amazon import build_mock_amazon
from app.connectors.mocks.flipkart import build_mock_flipkart
from app.orchestrator.agents.evaluation.hard_filter import hard_filter
from app.schemas.candidate import DataQuality
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from tests.unit.test_connectors import SHOES, make_auth, make_registry, store  # noqa: F401  (fixture)

BOTH = {"mock_amazon", "mock_flipkart"}
SEARCH_ONLY = frozenset({"search"})


class Counting:
    """Counts search calls reaching a connector."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.searches = 0

    id = property(lambda self: self._inner.id)
    domain = property(lambda self: self._inner.domain)
    capabilities = property(lambda self: self._inner.capabilities)

    async def execute(self, capability, request):
        self.searches += capability == "search"
        return await self._inner.execute(capability, request)


class Sink:
    def __init__(self) -> None:
        self.events = []

    def enqueue(self, event) -> None:
        self.events.append(event)


def sources(result) -> set[str]:
    return {c.source for c in result.candidates}


# ---- 1. empty response ------------------------------------------------------------------------


async def test_an_empty_answer_is_not_a_failure_and_is_not_retried(store):
    amazon = Counting(FlakyConnector(build_mock_amazon(), empty_search=True))
    registry = make_registry(store, amazon, build_mock_flipkart())
    result = await registry.search_resilient(make_auth(BOTH), SHOES, max_attempts=3)
    assert sources(result) == {"mock_flipkart"} and result.errors == () and result.failed_sources == ()
    assert (result.attempts, amazon.searches) == (1, 1)


async def test_every_platform_empty_returns_no_candidates_and_no_errors(store):
    registry = make_registry(
        store, FlakyConnector(build_mock_amazon(), empty_search=True), FlakyConnector(build_mock_flipkart(), empty_search=True)
    )
    result = await registry.search_resilient(make_auth(BOTH), SHOES)
    assert (result.candidates, result.errors, result.failed_sources) == ((), (), ())


# ---- 2. a connector that keeps failing --------------------------------------------------------


async def test_repeated_failures_stop_at_the_attempt_limit_and_keep_the_other_platforms_results(store):
    sink = Sink()
    amazon = Counting(FlakyConnector(build_mock_amazon(), fail_capabilities=SEARCH_ONLY))
    flipkart = Counting(build_mock_flipkart())
    registry = make_registry(store, amazon, flipkart)
    result = await registry.search_resilient(make_auth(BOTH), SHOES, max_attempts=3, audit=AuditScope(sink, "r1"))

    assert sources(result) == {"mock_flipkart"}
    assert result.failed_sources == ("mock_amazon",) and result.attempts == 3
    assert (amazon.searches, flipkart.searches) == (3, 1)  # only the failing platform is retried
    assert len(result.errors) == 1 and "mock_amazon" in result.errors[0]
    assert [e.action for e in sink.events if e.action == "SEARCH_ATTEMPTS_EXHAUSTED"] == ["SEARCH_ATTEMPTS_EXHAUSTED"]
    assert sink.events[-1].metadata_json == {"failed_sources": ["mock_amazon"]}


async def test_an_attempt_limit_of_one_does_not_retry(store):
    amazon = Counting(FlakyConnector(build_mock_amazon(), fail_capabilities=SEARCH_ONLY))
    registry = make_registry(store, amazon, build_mock_flipkart())
    result = await registry.search_resilient(make_auth(BOTH), SHOES, max_attempts=1)
    assert (result.attempts, amazon.searches, result.failed_sources) == (1, 1, ("mock_amazon",))


async def test_a_nonpositive_attempt_limit_is_refused(store):
    with pytest.raises(ValueError):
        await make_registry(store).search_resilient(make_auth(BOTH), SHOES, max_attempts=0)


async def test_retrying_never_widens_the_scope_to_an_unpermitted_platform(store):
    amazon = Counting(FlakyConnector(build_mock_amazon(), fail_capabilities=SEARCH_ONLY))
    flipkart = Counting(build_mock_flipkart())
    registry = make_registry(store, amazon, flipkart)
    result = await registry.search_resilient(make_auth({"mock_amazon"}), SHOES, max_attempts=3)
    assert result.candidates == () and flipkart.searches == 0


# ---- 3. incomplete candidate information ------------------------------------------------------


async def test_incomplete_candidates_are_kept_flagged_and_never_pass_a_hard_filter_on_the_missing_field(store):
    patchy = FlakyConnector(build_mock_flipkart(), blank_fields=frozenset({"total_cost", "rating"}))
    result = await make_registry(store, patchy).search_resilient(make_auth({"mock_flipkart"}), SHOES)
    assert result.candidates and result.errors == ()
    assert all(c.total_cost is None and c.quality["total_cost"] is DataQuality.MISSING for c in result.candidates)

    budget = Constraint(
        field_name="budget", operator=Operator.LTE, target_value=Decimal(100000), source=ConstraintSource.USER,
        strength=ConstraintStrength.HARD,
    )
    filtered = hard_filter(result.candidates, [budget])
    assert filtered.survivors == ()
    assert {r.kind.value for r in filtered.rejections} == {"unverifiable"}  # unknown price is not assumed affordable


# ---- 4. timeout after another platform answered -----------------------------------------------


class Hanging:
    id, domain = "mock_amazon", "shopping"
    capabilities = build_mock_amazon().capabilities

    async def execute(self, capability, request):
        await asyncio.Event().wait()  # asyncio.sleep is patched out by conftest, so block on an event


async def test_a_timeout_after_another_platform_answered_keeps_those_candidates(store):
    registry = make_registry(store, Hanging(), build_mock_flipkart())
    registry._timeout = 0.05
    result = await registry.search_resilient(make_auth(BOTH), SHOES, max_attempts=2)
    assert sources(result) == {"mock_flipkart"}
    assert result.failed_sources == ("mock_amazon",) and result.attempts == 2
    assert all("timed out" in e for e in result.errors)


# ---- 5. success after a retry -----------------------------------------------------------------


async def test_a_platform_that_recovers_on_retry_adds_its_results_without_duplicating_others(store):
    sink = Sink()
    amazon = Counting(FlakyConnector(build_mock_amazon(), fail_capabilities=SEARCH_ONLY, failures=1))
    flipkart = Counting(build_mock_flipkart())
    registry = make_registry(store, amazon, flipkart)
    result = await registry.search_resilient(make_auth(BOTH), SHOES, max_attempts=3, audit=AuditScope(sink, "r1"))

    assert sources(result) == BOTH
    assert result.failed_sources == () and result.errors == () and result.attempts == 2
    assert (amazon.searches, flipkart.searches) == (2, 1)
    keys = [(c.source, c.candidate_id) for c in result.candidates]
    assert len(keys) == len(set(keys))
    assert "SEARCH_ATTEMPTS_EXHAUSTED" not in [e.action for e in sink.events]
    assert [e.metadata_json["attempt"] for e in sink.events if e.action == "SEARCH_STARTED"] == [1, 2]
