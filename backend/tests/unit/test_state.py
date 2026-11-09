from decimal import Decimal

import pytest
from langgraph.graph import END, START, StateGraph

from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.constraints import Constraint, ConstraintSource, Operator, diff_constraints
from app.schemas.state import (
    STATE_VERSION,
    TaskState,
    make_initial_state,
    merge_constraints,
    validate_state_update,
)


def _auth(run_id="r1") -> TaskAuthorization:
    return TaskAuthorization(
        run_id=run_id, max_spend=Decimal(5000),
        permitted_platforms=frozenset({"shop"}), permitted_actions=frozenset({Action.SEARCH}),
    )


def _price() -> Constraint:
    return Constraint(field_name="price", operator=Operator.LT, target_value=5000, source=ConstraintSource.USER)


def test_initial_state_requires_matching_run_id():
    with pytest.raises(ValueError):
        make_initial_state(run_id="other", original_intent="x", authorization=_auth())
    state = make_initial_state(run_id="r1", original_intent="buy shoes", authorization=_auth(), constraints=[_price()])
    assert state["state_version"] == STATE_VERSION
    assert len(state["constraint_diffs"][0].added_constraints) == 1


def test_merge_constraints_enforces_revision_history_and_user_precedence():
    c1 = _price()
    c2 = c1.revise(reason="r", changed_by=ConstraintSource.USER, target_value=4000)
    assert merge_constraints([c1], [c2]) == [c2]
    assert merge_constraints([c2], [c2]) == [c2]  # idempotent replay
    with pytest.raises(ValueError, match="does not follow"):
        merge_constraints([c2], [c1])
    forged = c2.model_copy(update={"revision": 3, "history": ()})
    with pytest.raises(ValueError, match="history"):
        merge_constraints([c2], [forged])
    inferred = c1.revise(reason="guess", changed_by=ConstraintSource.INFERRED, target_value=9000)
    with pytest.raises(ValueError, match="user constraint"):
        merge_constraints([c1], [inferred])


def test_validate_state_update():
    validate_state_update("n", {"status": "x"}, frozenset({"status"}))
    with pytest.raises(ValueError, match="unknown"):
        validate_state_update("n", {"bogus": 1}, frozenset({"status"}))
    with pytest.raises(ValueError, match="may not write"):
        validate_state_update("n", {"plan": {}}, frozenset({"status"}))


async def test_graph_preserves_intent_and_authorization_and_aggregates():
    """Replanning node tries to overwrite protected fields; reducers must protect them."""
    original = _price()
    revised = original.revise(reason="user changed", changed_by=ConstraintSource.USER, target_value=4000)

    def replan(state: TaskState) -> dict:
        return {
            "original_intent": "something else",
            "authorization": _auth().model_copy(update={"max_spend": Decimal(10**6)}),
            "constraints": [revised],
            "constraint_diffs": [diff_constraints(state["constraints"], [revised])],
            "errors": ["replanned"],
            "status": "replanned",
        }

    graph = StateGraph(TaskState)
    graph.add_node("replan", replan)
    graph.add_edge(START, "replan")
    graph.add_edge("replan", END)
    app = graph.compile()

    auth = _auth()
    initial = make_initial_state(run_id="r1", original_intent="buy shoes", authorization=auth, constraints=[original])
    result = await app.ainvoke(initial)

    assert result["original_intent"] == "buy shoes"
    assert result["authorization"] == auth
    assert result["constraints"] == [revised] and result["constraints"][0].history[0].target_value == Decimal(5000)
    assert len(result["constraint_diffs"]) == 2
    assert result["errors"] == ["replanned"] and result["status"] == "replanned"
