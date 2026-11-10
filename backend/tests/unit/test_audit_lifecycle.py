"""Lifecycle audit coverage: intent, plan, discovery, evaluation and adapt each emit their standard events."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from app.audit.events import AuditScope
from app.connectors.flaky import FlakyConnector
from app.connectors.mocks.amazon import build_mock_amazon
from app.connectors.mocks.flipkart import build_mock_flipkart
from app.domains import get_plugin
from app.orchestrator.agents.adapt.node import adapt_node, resolve_decision
from app.orchestrator.agents.coordinator.plan import build_plan
from app.orchestrator.agents.evaluation.pipeline import evaluate
from app.orchestrator.agents.intent.agent import extract_intent, patch_intent
from app.schemas.adapt import UserDecision, UserDecisionKind
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from conftest import FakeProvider
from tests.unit.test_connectors import SHOES, make_auth, make_registry, store  # noqa: F401  (fixture)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHOP = get_plugin("shopping")


class Sink:
    def __init__(self) -> None:
        self.events = []

    def enqueue(self, event) -> None:
        self.events.append(event)

    def actions(self, phase: str | None = None) -> list[str]:
        return [e.action for e in self.events if phase is None or e.phase == phase]


def scope() -> tuple[Sink, AuditScope]:
    sink = Sink()
    return sink, AuditScope(sink, "r1")


def budget(value: int) -> Constraint:
    return Constraint(
        field_name="budget", operator=Operator.LTE, target_value=Decimal(value), source=ConstraintSource.USER,
        strength=ConstraintStrength.HARD,
    )


def candidate(cid: str, cost: int) -> Candidate:
    return Candidate(candidate_id=cid, source="t", domain="shopping", title=cid, total_cost=Decimal(cost), fetched_at=NOW)


def test_build_plan_audits_domain_and_plan_without_changing_its_result():
    sink, audit = scope()
    assert build_plan(SHOP, audit=audit) == build_plan(SHOP)
    assert [(e.phase, e.action) for e in sink.events] == [("domain", "DOMAIN_LOADED"), ("plan", "PLAN_CREATED")]
    assert sink.events[1].input_hash is not None


async def test_intent_extraction_and_patch_are_audited_with_hashed_input():
    sink, audit = scope()
    good = {"field": "budget", "operator": "lt", "value": 5000, "strength": "hard", "evidence": "under 5000"}
    provider = FakeProvider(json.dumps({"goal": "shoes", "constraints": [good]}))
    result, _ = await extract_intent("shoes under 5000", SHOP, provider=provider, audit=audit)
    assert sink.actions("intent") == ["INTENT_EXTRACTED"]
    assert "shoes under 5000" not in sink.events[0].model_dump_json()

    patch = {"ops": [{"op": "update", "field": "budget", "operator": "lt", "value": 4000, "evidence": "make it 4000"}]}
    await patch_intent("make it 4000", "shoes", list(result.constraints), SHOP, provider=FakeProvider(json.dumps(patch)), audit=audit)
    assert sink.actions("intent")[-1] == "CONSTRAINT_UPDATED"
    assert sink.events[-1].metadata_json == {"changes": [{"op": "update", "field": "budget"}]}


async def test_missing_blocking_field_audits_a_clarification_request():
    sink, audit = scope()
    contractor = get_plugin("contractor")
    provider = FakeProvider(json.dumps({"goal": "build a site", "constraints": []}))
    await extract_intent("build a site", contractor, provider=provider, audit=audit)
    assert sink.actions("intent") == ["INTENT_EXTRACTED", "USER_CLARIFICATION_REQUESTED"]


async def test_search_audits_start_results_and_connector_failure(store):
    sink, audit = scope()
    broken = FlakyConnector(build_mock_amazon(), fail_capabilities=frozenset({"search"}))
    registry = make_registry(store, broken, build_mock_flipkart())
    await registry.search_all(make_auth({"mock_amazon", "mock_flipkart"}), SHOES, audit=audit)

    by_action = {e.action: e for e in sink.events if e.phase == "discovery"}
    assert set(by_action) == {"SEARCH_STARTED", "CONNECTOR_FAILURE", "SEARCH_RESULTS_RECEIVED"}
    assert by_action["CONNECTOR_FAILURE"].resource_id == "mock_amazon"
    assert by_action["SEARCH_RESULTS_RECEIVED"].resource_id == "mock_flipkart"


def test_evaluation_audits_rejections_ranking_and_outcome_with_ids_only():
    sink, audit = scope()
    evaluate([candidate("cheap", 100), candidate("dear", 900)], [budget(500)], now=NOW, audit=audit)
    rejected = [e for e in sink.events if e.action == "CANDIDATE_REJECTED"]
    assert [e.resource_id for e in rejected] == ["dear"]
    assert sink.actions("evaluation")[-2:] == ["CANDIDATES_RANKED", "EVALUATION_COMPLETED"]
    assert all(e.phase == "evaluation" for e in sink.events)


def test_adapt_audits_the_attempt_and_the_user_decision():
    sink, audit = scope()
    state = {"candidates": [candidate("A", 9000)], "constraints": [budget(8000)]}
    adapt_node(state, plugin=SHOP, now=NOW, audit=audit)
    assert sink.actions("adapt")[0] == "ADAPTATION_ATTEMPTED"


async def test_user_decision_on_a_hard_constraint_is_audited():
    sink, audit = scope()
    state = {"candidates": [candidate("A", 9000)], "constraints": [budget(8000)]}
    await resolve_decision(UserDecision(kind=UserDecisionKind.DECLINE), state, plugin=SHOP, audit=audit)
    event = sink.events[0]
    assert (event.phase, event.action, event.decision, event.actor_type) == ("adapt", "USER_DECISION", "decline", "user")
