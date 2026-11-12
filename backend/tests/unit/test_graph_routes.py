"""Phase 2.3: end-to-end routes on the real compiled graph (blocked intent, near-tie choice, WIDEN, id collisions)."""

import json
from datetime import UTC, datetime


from conftest import FakeProvider
from langgraph.types import Command

from app.connectors.base import Capability, specs
from app.connectors.mocks.amazon import build_mock_amazon
from app.connectors.mocks.fixture import FixtureConnector
from app.domains.shopping import SHOPPING
from app.orchestrator.agents.discovery.agent import discovery_search_node
from app.orchestrator.graph.builder import _pick_candidate
from app.schemas.coordinator import Action as CoordinatorAction
from app.schemas.coordinator import CoordinatorProgress
from app.schemas.evaluator import EvaluationReport, EvaluatorOutcome, LLMStatus, RankedViable
from app.runs.view import question_text
from app.orchestrator.graph.runtime import RunSnapshot
from tests.unit.graph_harness import controller, extraction, initial
from tests.unit.test_connectors import MemoryTextStore, make_registry

CONFIG = {"configurable": {"thread_id": "r1"}}


async def _start(runs):
    """Start a run; past evaluation the mocked executor is reached, which these routing tests do not exercise."""
    try:
        await runs.start(initial())
    except Exception:
        pass


async def _settle(runs, value):
    """Resume; a later node may hit the mocked executor, which is outside these tests."""
    try:
        await runs.resume("r1", value)
    except Exception:
        pass
    return await runs.snapshot("r1")


async def test_blocked_intent_pauses_for_its_own_question_then_continues_with_the_answer():
    no_goal = extraction(goal="")
    provider = FakeProvider(no_goal, extraction("running shoes", budget=None), json.dumps({"ops": []}))
    runs = controller(provider)
    await runs.start(initial())
    snap = await runs.snapshot("r1")
    assert snap.next == ("ask_user",)  # used to route into Adapt -> discovery and crash on the empty goal
    assert snap.interrupts[0]["type"] == "clarification"
    assert question_text(snap) == SHOPPING.goal_question

    snap = await _settle(runs, "I need running shoes")
    assert snap.values["goal"] == "running shoes"
    assert [c.field_name for c in snap.values["constraints"] if c.active] == ["budget"]  # the intake budget is kept
    assert snap.values["candidates"]  # discovery ran with the clarified goal
    assert snap.values["original_intent"] == initial()["original_intent"]  # never replaced


async def test_reply_that_leaves_the_request_ambiguous_asks_again():
    provider = FakeProvider(extraction(goal=""), extraction(goal="", budget=None), json.dumps({"ops": []}))
    runs = controller(provider)
    await runs.start(initial())
    snap = await _settle(runs, "hmm")
    assert snap.next == ("ask_user",) and snap.values["status"] == "blocked"


async def test_unusable_reply_reasks_instead_of_crashing():
    runs = controller(FakeProvider(extraction(goal="")))  # the resume's LLM calls fail: provider has no answers left
    await runs.start(initial())
    snap = await _settle(runs, "running shoes")
    assert snap.next == ("ask_user",)
    assert snap.values["questions"][0].startswith("Sorry, I could not apply that answer")


def _report(*ids: str, needs_choice: bool) -> EvaluationReport:
    ranked = tuple(RankedViable(candidate_id=i, score=0.8, code_score=0.8) for i in ids)
    return EvaluationReport(
        outcome=EvaluatorOutcome.VIABLE, reason="near tie", attempt=0, ranked=ranked, margin=0.0, needs_choice=needs_choice,
        llm_status=LLMStatus.SKIPPED, evaluated_at=datetime.now(UTC),
    )


async def _paused_after_evaluate(report: EvaluationReport):
    runs = controller(FakeProvider(extraction()))
    await _start(runs)  # then re-enter right after evaluate with the report under test
    candidates = [listing.candidate for listing in build_mock_amazon()._listings.values()]
    await runs._graph.aupdate_state(CONFIG, {"evaluation": report, "candidates": candidates}, as_node="evaluate")
    try:
        await runs._invoke(runs._graph, None, CONFIG)
    except Exception:
        pass
    return runs


async def test_near_tie_pauses_for_the_users_choice_and_selects_it():
    runs = await _paused_after_evaluate(_report("amz-1", "amz-2", needs_choice=True))
    snap = await runs.snapshot("r1")
    assert snap.next == ("choose_candidate",) and "1." in question_text(snap)
    snap = await _settle(runs, "2")
    assert snap.values["selected_candidate_id"] == "amz-2"


async def test_clear_winner_is_selected_without_asking():
    runs = await _paused_after_evaluate(_report("amz-1", "amz-2", needs_choice=False))
    snap = await runs.snapshot("r1")
    assert "choose_candidate" not in snap.next and snap.values["selected_candidate_id"] == "amz-1"


def test_choice_parsing_accepts_numbers_and_ids_only():
    ranked = ["a", "b"]
    assert _pick_candidate("2", ranked) == "b" and _pick_candidate(" a ", ranked) == "a"
    assert _pick_candidate("3", ranked) is None and _pick_candidate("the cheap one", ranked) is None


async def test_coordinator_widen_goes_through_adapts_recovery_ladder():
    runs = controller(FakeProvider(extraction()))
    await _start(runs)
    progress = CoordinatorProgress(iterations=1, last_action=CoordinatorAction.WIDEN)
    await runs._graph.aupdate_state(CONFIG, {"coordinator": progress}, as_node="coordinator")
    assert (await runs.snapshot("r1")).next == ("adapt",)  # used to re-run identical discovery


async def test_same_candidate_id_on_two_platforms_keeps_the_first_and_reports_the_clash():
    amazon = build_mock_amazon()
    clone_listings = tuple(
        l.model_copy(update={"candidate": l.candidate.model_copy(update={"source": "mock_clone"})}) for l in amazon._listings.values()
    )
    clone = FixtureConnector(id="mock_clone", domain="shopping", capabilities=specs(*Capability), listings=clone_listings)
    registry = make_registry(MemoryTextStore(), amazon, clone)
    state = initial() | {"goal": "running shoes", "constraints": []}
    state["authorization"] = state["authorization"].model_copy(update={"permitted_platforms": frozenset({"mock_amazon", "mock_clone"})})
    first = await discovery_search_node(state, registry=registry, plugin=SHOPPING)
    advanced = first["discovery"].model_copy(update={"platform_index": 1, "plan": None, "plan_searched": False})
    second = await discovery_search_node(
        state | {"candidates": first["candidates"], "discovery": advanced}, registry=registry, plugin=SHOPPING
    )
    ids = [c.candidate_id for c in second["candidates"]]
    assert len(ids) == len(set(ids)) and {c.source for c in second["candidates"]} == {"mock_amazon"}
    assert any("already used by mock_amazon" in e for e in second["errors"])


def test_question_text_reads_choice_prompts():
    snap = RunSnapshot(values={}, next=("choose_candidate",), interrupts=({"message": "pick one"},))
    assert question_text(snap) == "pick one"


async def _resume_quietly(runs) -> None:
    try:
        await runs._invoke(runs._graph, None, CONFIG)
    except Exception:
        pass  # the harness's mocked session factory is not awaitable; the checkpoint is what matters


async def test_declined_product_lets_the_user_pick_another_instead_of_ending_the_run():
    runs = await _paused_after_evaluate(_report("amz-1", "amz-2", "amz-3", needs_choice=False))
    update = {"selected_candidate_id": "amz-1", "status": "approval_rejected", "pending_approval": None}
    await runs._graph.aupdate_state(CONFIG, update, as_node="await_approval")
    await _resume_quietly(runs)
    snap = await runs.snapshot("r1")
    assert snap.next == ("choose_candidate",) and snap.values["declined_candidate_ids"] == ["amz-1"]
    text = question_text(snap)
    assert "2." in text and "3." not in text  # the declined product is not offered again
    snap = await _settle(runs, "1")
    assert snap.values["selected_candidate_id"] == "amz-2"


async def test_declining_the_only_product_ends_the_run():
    runs = await _paused_after_evaluate(_report("amz-1", needs_choice=False))
    update = {"selected_candidate_id": "amz-1", "status": "approval_rejected", "pending_approval": None}
    await runs._graph.aupdate_state(CONFIG, update, as_node="await_approval")
    await _resume_quietly(runs)
    snap = await runs.snapshot("r1")
    assert not snap.next and snap.values["status"] in {"failed", "stopped"}


def test_choice_parsing_accepts_an_exact_title():
    assert _pick_candidate("beta phone", ["a", "b"], {"a": "Alpha", "b": "Beta Phone"}) == "b"
    assert _pick_candidate("phone", ["a", "b"], {"a": "Alpha", "b": "Beta Phone"}) is None
