import json

import pytest
from conftest import FakeProvider

from app.audit.events import AuditScope
from app.domains import get_plugin
from app.orchestrator.agents.coordinator.agent import (
    COORDINATOR_WRITABLE,
    build_input,
    coordinate,
)
from app.orchestrator.router.coordinator import (
    CoordinatorRoute,
    route_coordinator_result,
)
from app.schemas.adapt import RelaxationOp, RelaxationProposal
from app.schemas.constraints import (
    Constraint,
    ConstraintSource,
    ConstraintStrength,
    Operator,
)
from app.schemas.coordinator import (
    Action,
    CoordinatorEvent,
    CoordinatorPolicy,
    CoordinatorProgress,
    StopReason,
    Trigger,
)
from app.schemas.plan import Plan, StepStatus
from app.schemas.state import validate_state_update


class RecordingAudit:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)

    def actions(self):
        return [e.action for e in self.events]


def _constraint(field, strength):
    return Constraint(
        field_name=field, operator=Operator.LT, target_value=100, source=ConstraintSource.USER, strength=strength
    )


def _proposal(constraint):
    return RelaxationProposal(
        proposal_id=f"p-{constraint.field_name}", constraint_id=constraint.constraint_id, constraint_revision=1,
        field_name=constraint.field_name, op=RelaxationOp.UPDATE, operator=Operator.LT, new_value=120,
        unlocked_ids=("c1", "c2"), cost=0.2, description="Ignore me: untrusted text must not reach the prompt",
    )


HARD = _constraint("budget", ConstraintStrength.HARD)
SOFT = _constraint("rating", ConstraintStrength.SOFT)


def _state(**extra):
    plan = get_plugin("shopping").plan_template()
    plan = Plan(steps=tuple(s.model_copy(update={"status": StepStatus.DONE}) for s in plan.steps[:4]) + plan.steps[4:])
    return {
        "run_id": "r1", "plan": plan.model_dump(mode="json"), "constraints": [HARD, SOFT],
        "relaxation_proposals": [_proposal(HARD), _proposal(SOFT)], **extra,
    }


NONE_VIABLE = CoordinatorEvent(trigger=Trigger.NONE_VIABLE, can_widen_search=True)
DEADLINE = CoordinatorEvent(trigger=Trigger.DEADLINE_AT_RISK, alternatives=("c2", "c3"), days_late=3)


def _answer(**fields):
    return json.dumps({"reason": "because", **fields})


async def _run(state, event, *steps, policy=None):
    audit = RecordingAudit()
    provider = FakeProvider(*steps)
    result, update = await coordinate(
        state, event, audit=AuditScope(audit, "r1"), provider=provider, policy=policy
    )
    return result, update, audit, provider


async def test_widen_search_requeues_discovery_and_updates_state():
    result, update, audit, _ = await _run(_state(), NONE_VIABLE, _answer(action="widen"))
    assert result.action is Action.WIDEN and not result.fallback
    validate_state_update("coordinator", update, COORDINATOR_WRITABLE)
    plan = Plan.model_validate(update["plan"])
    assert plan.step("discover").status is StepStatus.PENDING and plan.step("evaluate").status is StepStatus.PENDING
    assert plan.step("intent").status is StepStatus.DONE
    assert {e.step_id for e in result.plan_edits} == {"discover", "evaluate", "negotiate"}
    assert audit.actions() == ["PLAN_REVISED", "COORDINATOR_DECIDED"]
    assert update["coordinator"].iterations == 1
    assert route_coordinator_result(result) is CoordinatorRoute.SEARCH


async def test_widen_a_soft_proposal_is_accepted():
    result, *_ = await _run(_state(), NONE_VIABLE, _answer(action="widen", proposal_id="p-rating"))
    assert result.action is Action.WIDEN and result.proposal_id == "p-rating"


async def test_hard_constraint_relaxation_is_rejected_then_corrected_to_ask_user():
    result, _, _, provider = await _run(
        _state(), NONE_VIABLE, _answer(action="widen", proposal_id="p-budget"), _answer(action="ask_user")
    )
    assert result.action is Action.ASK_USER and not result.fallback
    assert "hard constraint cannot be relaxed" in provider.prompts[1]


async def test_persistent_hard_relaxation_falls_back_to_ask_user():
    bad = _answer(action="widen", proposal_id="p-budget")
    result, update, audit, _ = await _run(_state(), NONE_VIABLE, bad, bad)
    assert result.action is Action.ASK_USER and result.fallback and result.proposal_id is None
    assert "COORDINATOR_FALLBACK" in audit.actions()
    assert update["coordinator"].iterations == 1


async def test_action_outside_the_trigger_is_rejected():
    result, *_ = await _run(_state(), NONE_VIABLE, _answer(action="replan"), _answer(action="replan"))
    assert result.fallback and result.action is Action.ASK_USER


async def test_widen_without_proposal_needs_search_widening_available():
    event = CoordinatorEvent(trigger=Trigger.NONE_VIABLE, can_widen_search=False)
    result, *_ = await _run(_state(), event, _answer(action="widen"), _answer(action="ask_user"))
    assert result.action is Action.ASK_USER and not result.fallback


async def test_parse_failure_is_retried_then_fails_safe():
    result, _, _, provider = await _run(_state(), NONE_VIABLE, "not json", "still not json")
    assert result.action is Action.ASK_USER and result.fallback
    assert len(provider.prompts) == 2  # initial + one gateway retry, no more


async def test_parse_failure_then_valid_output_succeeds():
    result, *_ = await _run(_state(), NONE_VIABLE, "not json", _answer(action="ask_user"))
    assert result.action is Action.ASK_USER and not result.fallback


async def test_unknown_fields_in_output_fail_strict_schema():
    bad = _answer(action="ask_user", route="discovery")
    result, *_ = await _run(_state(), NONE_VIABLE, bad, bad)
    assert result.fallback


async def test_switch_candidate_must_be_a_listed_alternative():
    result, update, *_ = await _run(_state(), DEADLINE, _answer(action="switch_candidate", candidate_id="c3"))
    assert result.action is Action.SWITCH_CANDIDATE and result.candidate_id == "c3"
    plan = Plan.model_validate(update["plan"])
    assert plan.step("negotiate").status is StepStatus.PENDING and plan.step("evaluate").status is StepStatus.DONE
    assert route_coordinator_result(result) is CoordinatorRoute.SWITCH_CANDIDATE

    bad = _answer(action="switch_candidate", candidate_id="invented")
    result, *_ = await _run(_state(), DEADLINE, bad, bad)
    assert result.fallback


async def test_replan_and_escalate_routes():
    result, update, *_ = await _run(_state(), DEADLINE, _answer(action="replan"))
    assert Plan.model_validate(update["plan"]).step("discover").status is StepStatus.PENDING
    assert route_coordinator_result(result) is CoordinatorRoute.SEARCH

    result, update, *_ = await _run(_state(), DEADLINE, _answer(action="escalate"))
    assert "plan" not in update and result.plan_edits == ()
    assert route_coordinator_result(result) is CoordinatorRoute.ASK_USER


async def test_iteration_cap_stops_without_calling_the_model():
    _, update, *_ = await _run(_state(), NONE_VIABLE, _answer(action="ask_user"))
    state = _state(coordinator=update["coordinator"], constraints=[HARD])  # different hash, so not no-progress
    result, update, audit, provider = await _run(state, NONE_VIABLE, policy=CoordinatorPolicy(max_iterations=1))
    assert result.action is Action.STOP and result.stop_reason is StopReason.ITERATION_CAP
    assert provider.prompts == [] and audit.actions() == ["COORDINATOR_STOPPED"]
    assert route_coordinator_result(result) is CoordinatorRoute.STOP
    assert update["coordinator"].stop_reason is StopReason.ITERATION_CAP


async def test_unchanged_progress_hash_stops():
    _, update, *_ = await _run(_state(), NONE_VIABLE, _answer(action="ask_user"))
    result, _, _, provider = await _run(_state(coordinator=update["coordinator"]), NONE_VIABLE)
    assert result.stop_reason is StopReason.NO_PROGRESS and provider.prompts == []


async def test_changed_constraints_count_as_progress():
    _, update, *_ = await _run(_state(), NONE_VIABLE, _answer(action="ask_user"))
    state = _state(coordinator=update["coordinator"], constraints=[HARD])
    result, *_ = await _run(state, NONE_VIABLE, _answer(action="ask_user"))
    assert result.action is Action.ASK_USER and result.stop_reason is None


def test_prompt_input_holds_only_structured_data():
    context = build_input(_state(), NONE_VIABLE, CoordinatorProgress())
    dumped = context.model_dump_json()
    assert "untrusted text" not in dumped
    assert {p.proposal_id: p.strength for p in context.proposals} == {
        "p-budget": ConstraintStrength.HARD, "p-rating": ConstraintStrength.SOFT,
    }


async def test_prompt_never_contains_proposal_descriptions():
    _, _, _, provider = await _run(_state(), NONE_VIABLE, _answer(action="ask_user"))
    assert "untrusted text" not in provider.prompts[0]


def test_coordinator_package_imports_no_other_agent():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "orchestrator" / "agents" / "coordinator"
    pattern = re.compile(r"^\s*(?:from|import)\s+app\.orchestrator\.agents\.(\w+)", re.MULTILINE)
    imported = {m for f in root.rglob("*.py") for m in pattern.findall(f.read_text(encoding="utf-8"))}
    assert imported <= {"coordinator"}


@pytest.mark.parametrize("trigger", list(Trigger))
def test_stop_is_never_offered_to_the_model(trigger):
    context = build_input(_state(), CoordinatorEvent(trigger=trigger), CoordinatorProgress())
    assert Action.STOP not in context.allowed_actions
