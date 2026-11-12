"""Discovery agent: request building, thin data, the guarded query ladder, the Router and the node-by-node loop.

``drive`` plays the graph: after every node it asks ``route_discovery`` what runs next, which is the
only thing that decides the flow. The nodes themselves never route.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.audit.events import AuditScope
from app.connectors.base import Capability, SearchRequest, specs
from app.connectors.base.models import Listing, ReviewText, SearchResponse
from app.connectors.flaky import FlakyConnector
from app.connectors.mocks.fixture import FixtureConnector
from app.domains import get_plugin
from app.llm.providers import LLMError
from app.orchestrator.agents.discovery.agent import (
    DISCOVERY_WRITABLE,
    discovery_adapt_node,
    discovery_advance_node,
    discovery_finish_node,
    discovery_search_node,
    discovery_start_node,
    searchable_platforms,
)
from app.orchestrator.agents.discovery.request import build_search_request, initial_plan
from app.orchestrator.agents.discovery.rewrite import QueryAdapter, QueryRewriter
from app.orchestrator.agents.discovery.thin import assess_thinness
from app.orchestrator.guard.query_guard import QueryRejected, validate_rewrite
from app.orchestrator.router.discovery import route_discovery
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from app.schemas.discovery import (
    DiscoveryPolicy,
    DiscoveryProgress,
    DiscoveryRoute,
    QueryPlan,
    QueryRewrites,
    QueryRung,
    StopReason,
    ThinReason,
    ThinReport,
)
from app.schemas.state import make_initial_state, validate_state_update
from conftest import FakeProvider
from tests.unit.test_connectors import make_auth, make_registry, store  # noqa: F401  (fixture)

SHOP = get_plugin("shopping")
NOW = datetime.now(UTC)


def _c(field, operator, value, strength=ConstraintStrength.HARD) -> Constraint:
    return Constraint(
        field_name=field, operator=operator, target_value=value, source=ConstraintSource.USER, strength=strength
    )


def plan(core, *soft) -> QueryPlan:
    return QueryPlan(core=core, soft_terms=soft)


def listing(cid, source, title, **fields) -> Listing:
    base = {
        "total_cost": Decimal("1000"), "currency": "INR", "rating": 4.0, "review_count": 10,
        "available_by": NOW + timedelta(days=2),
    }
    candidate = Candidate(candidate_id=cid, source=source, domain="shopping", title=title, **{**base, **fields})
    return Listing(candidate=candidate, description=f"{title} description")


class AllWords(FixtureConnector):
    """Search matches only titles containing every query word, so soft terms narrow the results."""

    async def execute(self, capability, request):
        if isinstance(request, SearchRequest):
            words = request.query.lower().split()
            hits = [item for item in self._listings.values() if all(w in item.candidate.title.lower() for w in words)]
            return SearchResponse(listings=tuple(self._fresh(h) for h in hits[: request.limit]))
        return await super().execute(capability, request)


def shop(source, *listings, **kw) -> AllWords:
    return AllWords(id=source, domain="shopping", capabilities=specs(*Capability), listings=tuple(listings), **kw)


class Counting:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.queries: list[str] = []

    id = property(lambda self: self._inner.id)
    domain = property(lambda self: self._inner.domain)
    capabilities = property(lambda self: self._inner.capabilities)

    async def execute(self, capability, request):
        if capability == "search":
            self.queries.append(request.query)
        return await self._inner.execute(capability, request)


class Sink:
    def __init__(self) -> None:
        self.events = []

    def enqueue(self, event) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.action for e in self.events]


def rewrites(*queries) -> str:
    return json.dumps({"queries": list(queries)})


POLICY = DiscoveryPolicy(min_candidates=2)


# ---- 1. request building --------------------------------------------------------------------------


def test_request_uses_goal_soft_terms_and_only_hard_equals_filters():
    constraints = [
        _c("brand", Operator.EQ, "Nike"),  # hard ==  -> filter
        _c("budget", Operator.LTE, Decimal("5000")),  # range  -> evaluation's job, not a filter
        _c("color", Operator.EQ, "black", ConstraintStrength.SOFT),  # soft -> query term
        _c("size", Operator.NE, "10"),
    ]
    request = build_search_request(initial_plan("running shoes", constraints, SHOP), constraints, SHOP, limit=7)
    assert (request.query, request.domain, request.limit) == ("running shoes black", "shopping", 7)
    assert request.filters == {"brand": "Nike"}


def test_request_requires_a_goal():
    with pytest.raises(ValueError, match="goal"):
        initial_plan("  ", [], SHOP)


# ---- 2. thin data ---------------------------------------------------------------------------------


def _cand(cid, **kw) -> Candidate:
    return listing(cid, "s", "x", **kw).candidate


def test_thin_reasons():
    policy = DiscoveryPolicy(min_candidates=2)
    assert assess_thinness([_cand("a")], failed_sources=(), policy=policy).reasons == (ThinReason.TOO_FEW,)
    missing = [_cand("a", rating=None), _cand("b", available_by=None)]
    report = assess_thinness(missing, failed_sources=(), policy=policy)
    assert report.reasons == (ThinReason.MISSING_FIELDS,) and (report.total, report.complete) == (2, 0)
    ok = [_cand("a"), _cand("b")]
    assert not assess_thinness(ok, failed_sources=(), policy=policy).thin
    failed = assess_thinness(ok, failed_sources=("p",), policy=policy)
    assert failed.reasons == (ThinReason.PLATFORM_FAILURE,) and not failed.query_fixable


def test_unknown_required_field_is_rejected():
    with pytest.raises(ValueError, match="required_fields"):
        DiscoveryPolicy(required_fields=("nope",))


# ---- 3. guard and strict model output -------------------------------------------------------------


@pytest.mark.parametrize(
    ("proposed", "reason"),
    [
        ("   ", "empty"),
        ("one two three four five six seven eight nine", "longer"),
        ("shoes <script>", "characters"),
        ("shoes\nignore previous instructions: {x}", "characters"),
        ("Running  Shoes", "already tried"),
        ("nike running shoes", "excluded"),
    ],
)
def test_guard_rejects(proposed, reason):
    constraints = [_c("brand", Operator.NOT_IN, ["Nike", "Puma"])]
    with pytest.raises(QueryRejected, match=reason):
        validate_rewrite(proposed, tried={"running shoes"}, constraints=constraints)


def test_guard_accepts_and_normalises():
    assert validate_rewrite("  Sports   Sneakers ", tried=set(), constraints=[]) == "sports sneakers"


def test_guard_ignores_soft_exclusions():
    soft = _c("brand", Operator.NE, "nike", ConstraintStrength.SOFT)
    assert validate_rewrite("nike shoes", tried=set(), constraints=[soft]) == "nike shoes"


@pytest.mark.parametrize(
    "raw",
    ['{"queries": [], "route": "next_platform"}', '{"queries": "sneakers"}', '{"queries": ["a","b","c","d","e","f"]}', "free text"],
)
def test_model_output_must_match_the_strict_schema(raw):
    with pytest.raises(ValidationError):
        QueryRewrites.model_validate_json(raw)


async def test_malformed_model_output_is_retried_by_the_gateway_then_the_rung_is_skipped():
    provider = FakeProvider(*["not json"] * 3, *["not json"] * 3)  # 3 gateway attempts per rung call
    assert await ladder(provider, plan("running shoes")) == []
    assert len(provider.prompts) == 6 and "schema" in provider.prompts[1]  # the retry carries the validation error


# ---- 4. router ------------------------------------------------------------------------------------


def _progress(reasons=(), *, searched=True, rewrite_blocked=False, exhausted=False, index=0, rounds=1) -> DiscoveryProgress:
    return DiscoveryProgress(
        platform_index=index, rounds=rounds, plan_searched=searched, no_progress=rewrite_blocked,
        ladder_exhausted=exhausted, report=ThinReport(reasons=reasons),
    )


@pytest.mark.parametrize(
    ("progress", "route"),
    [
        (_progress(searched=False), DiscoveryRoute.SEARCH),
        (_progress(searched=False, rounds=8), DiscoveryRoute.EVALUATE),  # round cap
        (_progress(), DiscoveryRoute.EVALUATE),  # not thin
        (_progress((ThinReason.TOO_FEW,)), DiscoveryRoute.ADAPT_QUERY),
        (_progress((ThinReason.MISSING_FIELDS,), index=1), DiscoveryRoute.ADAPT_QUERY),
        (_progress((ThinReason.TOO_FEW,), rewrite_blocked=True), DiscoveryRoute.NEXT_PLATFORM),
        (_progress((ThinReason.TOO_FEW,), exhausted=True), DiscoveryRoute.NEXT_PLATFORM),
        (_progress((ThinReason.PLATFORM_FAILURE,)), DiscoveryRoute.NEXT_PLATFORM),
        (_progress((ThinReason.TOO_FEW, ThinReason.PLATFORM_FAILURE)), DiscoveryRoute.NEXT_PLATFORM),
        (_progress((ThinReason.TOO_FEW,), rewrite_blocked=True, index=1), DiscoveryRoute.EVALUATE),  # last platform
        (_progress((ThinReason.TOO_FEW,), rounds=8), DiscoveryRoute.EVALUATE),  # capped while thin
    ],
)
def test_route_discovery(progress, route):
    assert route_discovery(progress, platform_count=2, policy=DiscoveryPolicy(max_rounds=8)) is route


# ---- 5. the ladder --------------------------------------------------------------------------------


async def ladder(provider, base, constraints=(), policy=POLICY):
    adapter = QueryAdapter(base, QueryRewriter(SHOP, constraints, provider=provider), policy)
    steps = []
    while (step := await adapter.next()) is not None:
        steps.append((step.rung, step.plan.text))
    return steps


async def test_ladder_order_is_synonyms_then_drop_soft_term_then_widen():
    provider = FakeProvider(rewrites("sneakers", "trainers"), rewrites("footwear"))
    assert await ladder(provider, plan("running shoes", "black", "waterproof")) == [
        (QueryRung.SYNONYMS, "sneakers black waterproof"),
        (QueryRung.SYNONYMS, "trainers black waterproof"),
        (QueryRung.DROP_SOFT_TERM, "running shoes black"),
        (QueryRung.DROP_SOFT_TERM, "running shoes"),
        (QueryRung.WIDEN_CATEGORY, "footwear"),
    ]


async def test_ladder_skips_drop_rung_without_soft_terms_and_never_repeats_a_query():
    # the widen proposal repeats a tried query: rejected, one corrective retry, rejected again, rung skipped
    provider = FakeProvider(rewrites("running shoes", "sneakers"), rewrites("sneakers"), rewrites("sneakers"))
    assert await ladder(provider, plan("running shoes")) == [(QueryRung.SYNONYMS, "sneakers")]


async def test_guard_rejections_never_reach_the_search():
    constraints = [_c("brand", Operator.NOT_IN, ["nike"])]
    provider = FakeProvider(rewrites("nike shoes", "ok shoes {x}", "plain shoes"), rewrites())
    assert await ladder(provider, plan("running shoes"), constraints) == [(QueryRung.SYNONYMS, "plain shoes")]


async def test_guard_rejecting_everything_gets_one_corrective_retry():
    constraints = [_c("brand", Operator.NOT_IN, ["nike"])]
    provider = FakeProvider(rewrites("nike shoes"), rewrites("sneakers"), rewrites())  # reject, retry ok, widen empty
    assert await ladder(provider, plan("running shoes"), constraints) == [(QueryRung.SYNONYMS, "sneakers")]
    assert "rejected" in provider.prompts[1] and "excluded" in provider.prompts[1]


async def test_guard_rejecting_the_retry_too_gives_up_on_the_rung():
    constraints = [_c("brand", Operator.NOT_IN, ["nike"])]
    provider = FakeProvider(rewrites("nike a"), rewrites("nike b"), rewrites("nike c"), rewrites("nike d"))
    assert await ladder(provider, plan("running shoes", "black"), constraints) == [
        (QueryRung.DROP_SOFT_TERM, "running shoes")
    ]
    assert len(provider.prompts) == 4  # one retry per model rung, no more


async def test_model_failure_skips_the_rung_not_the_ladder():
    provider = FakeProvider(LLMError("down"), LLMError("down"))
    assert await ladder(provider, plan("running shoes", "black")) == [(QueryRung.DROP_SOFT_TERM, "running shoes")]


async def test_the_model_prompt_carries_only_user_text():
    provider = FakeProvider(rewrites("sneakers"), rewrites())
    await ladder(provider, plan("running shoes"), [_c("brand", Operator.NOT_IN, ["nike"])])
    assert "running shoes" in provider.prompts[0] and "nike" in provider.prompts[0]


async def test_ladder_state_survives_a_save_and_resume():
    provider = FakeProvider(rewrites("sneakers", "trainers"), rewrites("footwear"))
    base = plan("running shoes", "black")
    rewriter = QueryRewriter(SHOP, [], provider=provider)
    first = QueryAdapter(base, rewriter, POLICY)
    assert (await first.next()).plan.text == "sneakers black"
    saved = first.state.model_copy()
    revived = QueryAdapter(
        base, QueryRewriter(SHOP, [], cache=rewriter.cache, provider=FakeProvider(rewrites("footwear"))), POLICY,
        type(saved).model_validate_json(saved.model_dump_json()),
    )
    rest = []
    while (step := await revived.next()) is not None:
        rest.append(step.plan.text)
    assert rest == ["trainers black", "running shoes", "footwear"]


# ---- 6. the node loop -----------------------------------------------------------------------------


class Run:
    def __init__(self, state, routes) -> None:
        self.state, self.routes = state, routes

    discovery = property(lambda self: self.state["discovery"])
    candidates = property(lambda self: tuple(self.state.get("candidates", [])))
    queries = property(lambda self: self.discovery.queries)
    rounds = property(lambda self: self.discovery.rounds)
    stop = property(lambda self: self.discovery.stop)
    errors = property(lambda self: tuple(self.state.get("errors", [])))
    unavailable_ids = property(lambda self: tuple(sorted(k.split(":", 1)[1] for k in self.discovery.unavailable)))


async def drive(registry, platforms, constraints=(), goal="trainers", provider=None, policy=POLICY, audit=None):
    """Play the graph: run a node, ask the Router what comes next, repeat until it says EVALUATE."""
    auth = make_auth(platforms)
    state = make_initial_state(run_id="r1", original_intent=goal, authorization=auth, constraints=list(constraints))
    state["goal"] = goal
    provider = provider or FakeProvider()
    count = len(searchable_platforms(registry, auth, "shopping"))

    def apply(node, update):
        validate_state_update(node, update, DISCOVERY_WRITABLE)
        for key, value in update.items():
            state[key] = [*state.get(key, []), *value] if key == "errors" else value

    apply("start", discovery_start_node(state, audit=audit))
    routes = []
    for _ in range(100):
        route = route_discovery(state["discovery"], platform_count=count, policy=policy, audit=audit)
        routes.append(route)
        match route:
            case DiscoveryRoute.SEARCH:
                apply("search", await discovery_search_node(state, registry=registry, plugin=SHOP, policy=policy, audit=audit))
            case DiscoveryRoute.ADAPT_QUERY:
                apply("adapt", await discovery_adapt_node(state, plugin=SHOP, policy=policy, provider=provider, audit=audit))
            case DiscoveryRoute.NEXT_PLATFORM:
                apply("advance", discovery_advance_node(state, audit=audit))
            case DiscoveryRoute.EVALUATE:
                apply("finish", discovery_finish_node(state, policy=policy, audit=audit))
                return Run(state, routes)
    raise AssertionError("the Router never reached EVALUATE")


_run = drive


async def test_sufficient_data_stops_at_the_first_platform(store):
    first = Counting(shop("p1", listing("a", "p1", "alpha trainers"), listing("b", "p1", "beta trainers")))
    second = Counting(shop("p2", listing("c", "p2", "gamma trainers")))
    out = await _run(make_registry(store, first, second), ["p1", "p2"])
    assert out.stop is StopReason.SUFFICIENT and {c.candidate_id for c in out.candidates} == {"a", "b"}
    assert (first.queries, second.queries) == (["trainers"], [])  # FakeProvider() has no steps: no LLM call
    assert out.routes == [DiscoveryRoute.SEARCH, DiscoveryRoute.EVALUATE] and out.state["status"] == "discovered"


async def test_thin_platform_is_adapted_with_a_synonym_before_moving_on(store):
    platform = shop(
        "p1", listing("a", "p1", "alpha trainers"), listing("b", "p1", "beta sneakers"), listing("c", "p1", "gamma sneakers")
    )
    out = await _run(
        make_registry(store, platform), ["p1"], provider=FakeProvider(rewrites("sneakers")),
        policy=DiscoveryPolicy(min_candidates=3),
    )
    assert out.queries == ("trainers", "sneakers") and out.rounds == 2
    assert {c.candidate_id for c in out.candidates} == {"a", "b", "c"} and out.stop is StopReason.SUFFICIENT
    assert out.routes == [
        DiscoveryRoute.SEARCH, DiscoveryRoute.ADAPT_QUERY, DiscoveryRoute.SEARCH, DiscoveryRoute.EVALUATE
    ]


async def test_soft_term_narrows_the_first_queries(store):
    platform = shop("p1", listing("a", "p1", "alpha trainers"), listing("b", "p1", "beta trainers"))
    soft = [_c("color", Operator.EQ, "zebra", ConstraintStrength.SOFT)]
    out = await _run(make_registry(store, platform), ["p1"], soft, provider=FakeProvider(rewrites("sneakers"), rewrites()))
    # base and the synonym find nothing (the progress hash repeats on the empty pool), so adaptation stops here
    assert out.queries[:2] == ("trainers zebra", "sneakers zebra")


async def test_no_progress_ends_adaptation_and_rewrites_are_cached_across_platforms(store):
    provider = FakeProvider(rewrites("sneakers"))  # one scripted answer: the second platform must reuse it
    registry = make_registry(store, shop("p1", listing("z", "p1", "zzz")), shop("p2", listing("y", "p2", "yyy")))
    out = await _run(registry, ["p1", "p2"], provider=provider)
    assert out.queries == ("trainers", "sneakers", "trainers", "sneakers")
    assert len(provider.prompts) == 1 and out.stop is StopReason.EXHAUSTED and out.candidates == ()
    assert out.state["status"] == "discovery_empty"


async def test_round_cap_stops_the_loop(store):
    registry = make_registry(store, shop("p1", listing("a", "p1", "alpha trainers")))
    out = await _run(registry, ["p1"], provider=FakeProvider(), policy=DiscoveryPolicy(min_candidates=2, max_rounds=1))
    assert (out.rounds, out.stop) == (1, StopReason.ROUND_CAP)  # no model call: FakeProvider() has no steps


async def test_platform_failure_moves_to_the_next_platform_without_rewriting(store):
    broken = FlakyConnector(shop("p1", listing("a", "p1", "alpha trainers")), fail_capabilities=frozenset({"search"}))
    good = shop("p2", listing("b", "p2", "beta trainers"), listing("c", "p2", "gamma trainers"))
    out = await _run(make_registry(store, broken, good), ["p1", "p2"])  # FakeProvider() has no steps
    assert {c.candidate_id for c in out.candidates} == {"b", "c"} and out.stop is StopReason.SUFFICIENT
    assert any("p1" in e for e in out.errors) and DiscoveryRoute.NEXT_PLATFORM in out.routes


async def test_out_of_stock_candidates_are_dropped_and_never_re_enter(store):
    platform = shop(
        "p1", listing("a", "p1", "alpha trainers"), listing("b", "p1", "beta trainers"), out_of_stock=frozenset({"a"})
    )
    out = await _run(make_registry(store, platform), ["p1"], provider=FakeProvider(rewrites("sneakers")))
    assert [c.candidate_id for c in out.candidates] == ["b"] and out.unavailable_ids == ("a",)


async def test_enrichment_fills_missing_rating_from_reviews_and_clears_the_flag(store):
    bare = listing("a", "p1", "alpha trainers", rating=None, review_count=None)
    reviews = {"a": (ReviewText(rating=4, text="good", posted_at=NOW), ReviewText(rating=5, text="great", posted_at=NOW))}
    out = await _run(make_registry(store, shop("p1", bare, reviews=reviews)), ["p1"], policy=DiscoveryPolicy(min_candidates=1))
    (candidate,) = out.candidates
    assert (candidate.rating, candidate.review_count) == (4.5, 2)
    assert "rating" not in candidate.quality and len(candidate.text_refs) >= 3  # listing text + 2 review refs, ids only


async def test_enrichment_failure_keeps_the_candidate(store):
    inner = shop("p1", listing("a", "p1", "alpha trainers"))
    flaky = FlakyConnector(inner, fail_capabilities=frozenset({"reviews", "details"}))
    out = await _run(make_registry(store, flaky), ["p1"], policy=DiscoveryPolicy(min_candidates=1))
    assert [c.candidate_id for c in out.candidates] == ["a"] and len(out.errors) == 2


async def test_scope_never_widens(store):
    registry = make_registry(store, shop("p1", listing("a", "p1", "alpha trainers")), shop("p2", listing("b", "p2", "b trainers")))
    out = await _run(registry, ["p1"], policy=DiscoveryPolicy(min_candidates=1))  # auth permits p1 only
    assert {c.source for c in out.candidates} == {"p1"}


async def test_no_searchable_platform_ends_with_an_empty_pool(store):
    out = await _run(make_registry(store, shop("p1", listing("a", "p1", "alpha trainers"))), ["other"])
    assert out.candidates == () and out.stop is StopReason.EXHAUSTED and out.rounds == 0


async def test_progress_round_trips_through_json_so_a_checkpoint_can_resume(store):
    registry = make_registry(store, shop("p1", listing("z", "p1", "zzz")), shop("p2", listing("y", "p2", "yyy")))
    out = await _run(registry, ["p1", "p2"], provider=FakeProvider(rewrites("sneakers")))
    revived = DiscoveryProgress.model_validate_json(out.discovery.model_dump_json())
    assert revived == out.discovery and revived.rewrite_cache


async def test_nodes_write_only_owned_fields_and_start_keeps_the_pool(store):
    registry = make_registry(store, shop("p1", listing("a", "p1", "alpha trainers"), listing("b", "p1", "beta trainers")))
    out = await _run(registry, ["p1"])
    restart = discovery_start_node(out.state)
    validate_state_update("start", restart, DISCOVERY_WRITABLE)
    assert restart["discovery"] == DiscoveryProgress() and "candidates" not in restart and len(out.candidates) == 2


# ---- 7. audit -------------------------------------------------------------------------------------


async def test_every_step_is_audited(store):
    p1 = shop("p1", listing("a", "p1", "alpha trainers"), listing("b", "p1", "beta trainers"), out_of_stock=frozenset({"a"}))
    p2 = shop("p2", listing("c", "p2", "gamma trainers"), listing("d", "p2", "delta trainers"))
    sink = Sink()
    out = await _run(
        make_registry(store, p1, p2), ["p1", "p2"], provider=FakeProvider(rewrites("sneakers")),
        audit=AuditScope(sink, "r1"),
    )
    assert out.stop is StopReason.SUFFICIENT
    actions = set(sink.actions())
    assert actions >= {
        "DISCOVERY_STARTED", "DISCOVERY_ROUTED", "SEARCH_STARTED", "SEARCH_RESULTS_RECEIVED", "CANDIDATE_ENRICHED",
        "CANDIDATE_UNAVAILABLE", "DISCOVERY_ROUND_COMPLETED", "QUERY_REWRITE_PROPOSED", "QUERY_ADAPTED",
        "PLATFORM_ADVANCED", "DISCOVERY_COMPLETED",
    }
    routed = [e for e in sink.events if e.action == "DISCOVERY_ROUTED"]
    assert [e.decision for e in routed] == [r.value for r in out.routes]  # every routing decision is on the record
    assert all(e.phase == "discovery" for e in sink.events if e.action.startswith(("DISCOVERY_", "QUERY_", "CANDIDATE_")))


async def test_enrichment_skips_capabilities_the_platform_does_not_offer(store):
    from app.connectors.mocks.flipkart import build_mock_flipkart
    from app.orchestrator.agents.discovery.enrich import enrich_candidate

    full = build_mock_flipkart()
    no_reviews = FixtureConnector(
        id=full.id, domain=full.domain, capabilities=specs(Capability.SEARCH, Capability.DETAILS),
        listings=tuple(full._listings.values()),
    )
    registry = make_registry(store, no_reviews)
    candidate = next(iter(full._listings.values())).candidate
    errors: list[str] = []
    enriched = await enrich_candidate(
        registry, make_auth({full.id}), candidate, destination=None, reviews_limit=5, errors=errors
    )
    assert enriched is not None and errors == []
    assert registry.supports(full.id, Capability.DETAILS) and not registry.supports(full.id, Capability.REVIEWS)
    assert not registry.supports("nope", Capability.SEARCH)
