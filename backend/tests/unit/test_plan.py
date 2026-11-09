import pytest

from app.domains import get_plugin
from app.orchestrator.agents.coordinator.plan import (
    COORDINATOR_WRITABLE,
    EditOp,
    PlanEdit,
    build_plan,
    replan,
)
from app.schemas.plan import Plan, PlanStep, StepStatus
from app.schemas.state import validate_state_update


class RecordingAudit:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)


@pytest.mark.parametrize("domain", ["shopping", "contractor", "a2a"])
def test_every_plugin_template_is_a_valid_plan(domain):
    update = build_plan(get_plugin(domain))
    validate_state_update("coordinator", update, COORDINATOR_WRITABLE)
    plan = Plan.model_validate(update["plan"])
    assert plan.steps[0].depends_on == () and all(s.status is StepStatus.PENDING for s in plan.steps)


def test_plan_rejects_unknown_dependency_self_dependency_cycle_and_duplicates():
    with pytest.raises(ValueError, match="unknown or later"):
        Plan(steps=(PlanStep(step_id="a", description="a", depends_on=("b",)),))
    with pytest.raises(ValueError, match="unknown or later"):
        Plan(steps=(PlanStep(step_id="a", description="a", depends_on=("a",)),))
    with pytest.raises(ValueError, match="duplicate"):
        Plan(steps=(PlanStep(step_id="a", description="a"), PlanStep(step_id="a", description="again")))


def test_replan_edits_plan_and_writes_audit_event():
    plan = get_plugin("shopping").plan_template()
    audit = RecordingAudit()
    edits = [
        PlanEdit(op=EditOp.SET_STATUS, step_id="intent", status=StepStatus.DONE),
        PlanEdit(
            op=EditOp.ADD,
            step_id="compare",
            step=PlanStep(step_id="compare", description="Compare prices", depends_on=("discover",)),
            after="discover",
        ),
    ]
    update = replan(plan, edits, run_id="r1", reason="user asked to compare prices", audit=audit)
    revised = Plan.model_validate(update["plan"])
    assert [s.step_id for s in revised.steps][:3] == ["intent", "discover", "compare"]
    assert revised.step("intent").status is StepStatus.DONE
    (event,) = audit.events
    assert event.action == "PLAN_REVISED" and event.run_id == "r1"
    assert event.reason == "user asked to compare prices"
    assert len(event.state_diff["after"]["steps"]) == len(event.state_diff["before"]["steps"]) + 1


def test_invalid_replan_changes_nothing_and_logs_nothing():
    plan = get_plugin("shopping").plan_template()
    audit = RecordingAudit()
    with pytest.raises(ValueError):
        replan(plan, [PlanEdit(op=EditOp.REMOVE, step_id="discover")], run_id="r1", reason="x", audit=audit)
    with pytest.raises(ValueError, match="unknown step"):
        replan(plan, [PlanEdit(op=EditOp.REMOVE, step_id="nope")], run_id="r1", reason="x", audit=audit)
    assert audit.events == []
