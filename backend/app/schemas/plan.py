"""Plan: the Coordinator's ordered list of steps, stored in ``TaskState.plan``.

A plan is a validated DAG. Replanning is an edit of this structure (see
``orchestrator.agents.coordinator.plan``); there is no separate Planner agent.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StepStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING


class Plan(BaseModel):
    """Ordered steps; every dependency must exist, precede its dependant, and be acyclic."""

    model_config = ConfigDict(frozen=True)

    steps: tuple[PlanStep, ...]

    @model_validator(mode="after")
    def _check_graph(self) -> Self:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate step id {step.step_id!r}")
            unknown = [dep for dep in step.depends_on if dep not in seen]
            if unknown:
                # Dependencies must already appear earlier: this rejects unknown ids,
                # self-dependencies and cycles in one check, and keeps the list ordered.
                raise ValueError(f"step {step.step_id!r} depends on unknown or later steps: {unknown}")
            seen.add(step.step_id)
        return self

    def step(self, step_id: str) -> PlanStep:
        for candidate in self.steps:
            if candidate.step_id == step_id:
                return candidate
        raise KeyError(step_id)
