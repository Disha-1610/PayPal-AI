"""End-to-end Phase 4 flow through a real LangGraph graph: adapt -> interrupt() -> user reply -> resume."""

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.domains.contractor import CONTRACTOR
from app.orchestrator.agents.adapt.node import ADAPT_WRITABLE, adapt_node, ask_user_node, parse_decision
from app.schemas.adapt import RecoveryStep, TerminationReason, UserDecisionKind
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from app.schemas.state import TaskState, validate_state_update

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUDGET = Constraint(
    field_name="budget", operator=Operator.LTE, target_value=Decimal(8000), source=ConstraintSource.USER,
    strength=ConstraintStrength.HARD,
)
POOL = [
    Candidate(candidate_id="A", source="t", domain="contractor", title="A", total_cost=Decimal(9000), fetched_at=NOW),
    Candidate(candidate_id="B", source="t", domain="contractor", title="B", total_cost=Decimal(9500), fetched_at=NOW),
]


def _graph():
    async def adapt(state: TaskState):
        update = adapt_node(state, plugin=CONTRACTOR, now=NOW)
        validate_state_update("adapt", update, ADAPT_WRITABLE)
        return update

    async def ask(state: TaskState):
        return await ask_user_node(state, plugin=CONTRACTOR)

    def route(state: TaskState) -> str:
        step = state["adapt"].next_step
        return "ask_user" if step is RecoveryStep.ASK_USER else END

    builder = StateGraph(TaskState)
    builder.add_node("adapt", adapt)
    builder.add_node("ask_user", ask)
    builder.add_edge(START, "adapt")
    builder.add_conditional_edges("adapt", route, ["ask_user", END])
    builder.add_edge("ask_user", END)
    return builder.compile(checkpointer=InMemorySaver())


async def _run_until_ask(graph, thread):
    """The planner first schedules its search rungs; walk them (a search that finds nothing) up to the question."""
    cfg = {"configurable": {"thread_id": thread}}
    result = await graph.ainvoke({"candidates": POOL, "constraints": [BUDGET]}, cfg)
    while "__interrupt__" not in result:
        assert result["adapt"].next_step in (RecoveryStep.LOCAL_RECOVERY, RecoveryStep.CROSS_DOMAIN)
        result = await graph.ainvoke(Command(goto="adapt"), cfg)
    return cfg, result


async def test_interrupt_carries_proposals_and_accept_relaxes_budget_as_user():
    graph = _graph()
    cfg, paused = await _run_until_ask(graph, "t1")
    payload = paused["__interrupt__"][0].value
    assert payload["type"] == "relaxation_request" and payload["proposals"][0]["unlocks"] == 2
    top = payload["proposals"][0]["proposal_id"]

    final = await graph.ainvoke(Command(resume={"kind": "accept", "proposal_id": top}), cfg)
    budget = final["constraints"][0]
    assert budget.target_value == Decimal(9500) and budget.source is ConstraintSource.USER
    assert budget.history[-1].target_value == Decimal(8000) and budget.revision == 2
    assert final["relaxation_proposals"] == []


async def test_decline_and_cancel_terminate_without_touching_constraints():
    for kind, reason in (("decline", TerminationReason.USER_DECLINED), ("cancel", TerminationReason.CANCELLED)):
        graph = _graph()
        cfg, _ = await _run_until_ask(graph, kind)
        final = await graph.ainvoke(Command(resume={"kind": kind}), cfg)
        assert final["adapt"].termination is reason
        assert final["constraints"] == [BUDGET]


def test_decision_parsing():
    assert parse_decision("make it 9k").kind is UserDecisionKind.REPLY
    assert parse_decision({"kind": "cancel"}).kind is UserDecisionKind.CANCEL
    with pytest.raises(ValueError):
        parse_decision({"kind": "accept"})  # proposal_id required
    with pytest.raises(ValueError):
        parse_decision(42)


async def test_nodes_cannot_write_outside_their_fields():
    with pytest.raises(ValueError, match="may not write"):
        validate_state_update("adapt", {"order_id": "x"}, ADAPT_WRITABLE)


async def test_free_text_reply_goes_through_intent_patch_as_a_user_change():
    from app.orchestrator.agents.adapt.node import resolve_decision
    from app.schemas.adapt import UserDecision
    from conftest import FakeProvider

    patch = json.dumps(
        {"ops": [{"op": "update", "field": "budget", "operator": "lte", "value": 9500, "evidence": "9500"}]}
    )
    state = {"goal": "renovate", "constraints": [BUDGET], "candidates": POOL, "relaxation_proposals": []}
    update = await resolve_decision(
        UserDecision(kind=UserDecisionKind.REPLY, text="ok up to 9500"), state, plugin=CONTRACTOR,
        provider=FakeProvider(patch),
    )
    revised = update["constraints"][0]
    assert revised.target_value == Decimal(9500) and revised.source is ConstraintSource.USER
    assert update["relaxation_proposals"] == [] and update["constraint_diffs"][0].modified_constraints
