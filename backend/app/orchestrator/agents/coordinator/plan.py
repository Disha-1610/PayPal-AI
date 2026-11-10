"""Coordinator planning: the plan lives in ``TaskState.plan``; replanning is editing it, and every
edit is written to the audit log. There is no separate Planner agent.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from app.audit.events import AuditScope, AuditSink, Phase
from app.audit.service import AuditEvent, hash_input
from app.domains.base import DomainPlugin
from app.schemas.plan import Plan, PlanStep, StepStatus

# Fields this node may write into TaskState (checked with ``validate_state_update``).
COORDINATOR_WRITABLE = frozenset({"plan", "coordinator"})


class EditOp(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    SET_STATUS = "set_status"


class PlanEdit(BaseModel):
    model_config = ConfigDict(frozen=True)

    op: EditOp
    step_id: str
    step: PlanStep | None = None
    status: StepStatus | None = None
    after: str | None = None  # ADD: insert after this step id; default appends

    @model_validator(mode="after")
    def _check_shape(self) -> "PlanEdit":
        if self.op is EditOp.ADD and self.step is None:
            raise ValueError("add requires a step")
        if self.op is EditOp.SET_STATUS and self.status is None:
            raise ValueError("set_status requires a status")
        return self


def build_plan(plugin: DomainPlugin, *, audit: AuditScope | None = None) -> dict[str, object]:
    """Initial ``TaskState`` update: the plug-in's plan template."""
    plan = plugin.plan_template()
    if audit:
        audit.emit(Phase.DOMAIN, "DOMAIN_LOADED", node="coordinator", decision="allow", resource_type="domain", resource_id=plugin.name)
        audit.emit(
            Phase.PLAN, "PLAN_CREATED", node="coordinator", decision="allow", payload=plan.model_dump(mode="json"),
            metadata={"domain": plugin.name, "steps": [s.step_id for s in plan.steps]},
        )
    return {"plan": plan.model_dump(mode="json")}


def replan(
    current: Plan, edits: list[PlanEdit], *, run_id: str, reason: str, audit: AuditSink
) -> dict[str, object]:
    """Apply ``edits`` to ``current``, validate the result, log it, and return the state update.

    Raises ``ValueError`` (nothing logged, nothing changed) if an edit is invalid or the edited
    plan breaks the DAG (unknown dependency, cycle, duplicate id).
    """
    steps = list(current.steps)
    for edit in edits:
        index = next((i for i, s in enumerate(steps) if s.step_id == edit.step_id), None)
        if edit.op is EditOp.ADD:
            if edit.step is None or edit.step.step_id != edit.step_id:
                raise ValueError("add edit's step must match step_id")
            anchor = next((i for i, s in enumerate(steps) if s.step_id == edit.after), None) if edit.after else None
            if edit.after and anchor is None:
                raise ValueError(f"unknown step {edit.after!r} to insert after")
            steps.insert(len(steps) if anchor is None else anchor + 1, edit.step)
        elif index is None:
            raise ValueError(f"unknown step {edit.step_id!r}")
        elif edit.op is EditOp.REMOVE:
            del steps[index]
        else:
            assert edit.status is not None  # guaranteed by PlanEdit validation
            steps[index] = steps[index].model_copy(update={"status": edit.status})

    revised = Plan(steps=tuple(steps))  # validates the DAG
    audit.enqueue(
        AuditEvent(
            run_id=run_id,
            phase=Phase.PLAN,
            node="coordinator",
            actor_type="agent",
            action="PLAN_REVISED",
            resource_type="plan",
            input_hash=hash_input([e.model_dump(mode="json") for e in edits]),
            reason=reason,
            state_diff={
                "before": current.model_dump(mode="json"),
                "after": revised.model_dump(mode="json"),
            },
        )
    )
    return {"plan": revised.model_dump(mode="json")}
