"""Guard nodes, global limits, the stop node, and the structural rule that Guards sit on every money path."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from langgraph.checkpoint.memory import MemorySaver

from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.stop import stop_report_node
from app.orchestrator.guard.graph_guard import (
    GUARD_BLOCKED,
    GUARD_PASSED,
    guard_discovery_data,
    guard_execution,
    guard_payment,
)
from app.orchestrator.router.limits import GraphLimits, LimitRoute, StopCause, breached, route_limits
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action
from app.schemas.candidate import Candidate
from app.schemas.state import TaskState
from tests.unit.test_payment_service import AUTH, KEYS, TERMS, VERIFIER, signed

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def cand(cid="shoe-1", source="shop", cost="5200") -> Candidate:
    return Candidate(candidate_id=cid, source=source, domain="shopping", title="t", total_cost=Decimal(cost), currency="INR")


def state(**kw) -> TaskState:
    base = {"run_id": "r1", "authorization": AUTH, "candidates": [cand()], "selected_candidate_id": "shoe-1"}
    return TaskState(**{**base, **kw})


# ---- global limits ----------------------------------------------------------------------------------


def test_limits_continue_cap_and_no_progress():
    limits = GraphLimits(max_cycles=2)
    assert route_limits(state(cycles=1, cycle_hashes=["a"]), limits) is LimitRoute.CONTINUE
    assert breached(state(cycles=3, cycle_hashes=["a", "b", "c"]), limits) is StopCause.ITERATION_CAP
    assert breached(state(cycles=2, cycle_hashes=["a", "b", "a"]), limits) is StopCause.NO_PROGRESS
    assert route_limits(state(cycles=2, cycle_hashes=["a", "b", "a"]), limits) is LimitRoute.STOP
    assert route_limits(state(), limits) is LimitRoute.CONTINUE  # fresh task: no counters yet


# ---- guards -----------------------------------------------------------------------------------------


def test_discovery_guard_drops_unpermitted_platforms_and_blocks_expired_authorization():
    s = state(candidates=[cand("a"), cand("b", source="rogue")])
    update = guard_discovery_data(s, now=NOW)
    assert update["status"] == GUARD_PASSED and [c.candidate_id for c in update["candidates"]] == ["a"]
    expired = AUTH.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
    assert guard_discovery_data({**s, "authorization": expired}, now=NOW)["status"] == GUARD_BLOCKED


def test_execution_guard_checks_pool_platform_price_and_budget():
    assert guard_execution(state(), now=NOW)["status"] == GUARD_PASSED  # approval is asked later, so not required here
    assert guard_execution(state(selected_candidate_id="nope"), now=NOW)["status"] == GUARD_BLOCKED
    assert guard_execution(state(candidates=[cand(source="rogue")]), now=NOW)["status"] == GUARD_BLOCKED
    assert guard_execution(state(candidates=[cand(cost="9000")]), now=NOW)["status"] == GUARD_BLOCKED  # > max_spend
    unpriced = cand().model_copy(update={"total_cost": None})
    assert guard_execution(state(candidates=[unpriced]), now=NOW)["status"] == GUARD_BLOCKED


def test_payment_guard_requires_genuine_approval_for_exactly_these_terms():
    ok = state(approvals=[signed(Action.CREATE_ORDER)])
    assert guard_payment(ok, terms=TERMS, verifier=VERIFIER, now=datetime.now(UTC))["status"] == GUARD_PASSED
    other = ApprovalTerms(subject_id="shoe-1", parties={"payee_id": "m1"}, currency="INR", max_total_minor=999999)
    assert guard_payment(ok, terms=other, verifier=VERIFIER, now=datetime.now(UTC))["status"] == GUARD_BLOCKED  # hash mismatch
    assert guard_payment(state(approvals=[]), terms=TERMS, verifier=VERIFIER, now=datetime.now(UTC))["status"] == GUARD_BLOCKED
    assert guard_payment(ok, terms=TERMS, verifier=lambda _a: False, now=datetime.now(UTC))["status"] == GUARD_BLOCKED


# ---- stop node --------------------------------------------------------------------------------------


def test_stop_report_names_the_cause_and_preserves_failure():
    limits = GraphLimits(max_cycles=1)
    capped = stop_report_node(state(cycles=2, status="discovering"), limits=limits)
    assert (capped["status"], capped["stop_reason"]) == ("stopped", "iteration_cap")
    failed = stop_report_node(state(status="failed"), limits=limits)
    assert (failed["status"], failed["stop_reason"]) == ("failed", "failed")
    assert stop_report_node(state(status="guard_blocked"), limits=limits)["status"] == "stopped"


# ---- structure --------------------------------------------------------------------------------------


def edges_into(target: str) -> set[str]:
    deps = GraphDeps(
        plugin=MagicMock(), registry=MagicMock(), text_source=MagicMock(), executor=MagicMock(),
        session_factory=MagicMock(), audit_sink=MagicMock(), terms_for=MagicMock(), approval_keys=KEYS,
    )
    return {e.source for e in build_graph(deps, MemorySaver()).get_graph().edges if e.target == target}


def test_every_path_into_evaluator_executor_and_payment_goes_through_a_guard():
    assert "guard_evaluate" in edges_into("evaluate")
    assert edges_into("recheck") == {"guard_execute", "propose_plan"}
    assert edges_into("propose_plan") == {"guard_execute"}  # milestone domains: plan first, then the same recheck
    assert edges_into("place_order") == {"guard_pay"}
    assert edges_into("milestone_pay") == {"guard_pay"}
    assert edges_into("milestone_create") == {"guard_plan"}
    assert edges_into("guard_pay") == {"await_approval"}
    assert edges_into("guard_plan") == {"await_approval"}
    assert "discovery_finish" not in edges_into("evaluate")


def test_all_stops_converge_on_stop_report():
    sources = edges_into("stop_report")
    assert {"guard_evaluate", "guard_execute", "guard_pay", "discovery_start", "coordinator", "adapt", "intent"} <= sources
    assert edges_into("__end__") >= {"stop_report", "settle"}
