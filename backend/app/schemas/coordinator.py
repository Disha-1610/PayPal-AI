"""Coordinator models. The Coordinator reads structured state only and answers with a strict choice.

Nothing here carries untrusted text: candidates appear as opaque ids, constraints as field/operator/
strength, proposals as ids and counts. The model's only free text is ``reason``, which is audit-only
and never read by routing code.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.constraints import ConstraintStrength


class Trigger(StrEnum):
    NONE_VIABLE = "none_viable"  # evaluation left no viable candidate
    DEADLINE_AT_RISK = "deadline_at_risk"  # Monitor reported a deadline problem


class Action(StrEnum):
    WIDEN = "widen"  # relax a soft preference, or widen the search
    ASK_USER = "ask_user"
    REPLAN = "replan"
    SWITCH_CANDIDATE = "switch_candidate"  # next-ranked candidate
    ESCALATE = "escalate"
    STOP = "stop"  # set by code only (iteration cap / no progress), never by the model


# What the model may choose per trigger. ``STOP`` is never offered.
ALLOWED_ACTIONS: dict[Trigger, frozenset[Action]] = {
    Trigger.NONE_VIABLE: frozenset({Action.WIDEN, Action.ASK_USER}),
    Trigger.DEADLINE_AT_RISK: frozenset({Action.REPLAN, Action.SWITCH_CANDIDATE, Action.ESCALATE}),
}


class StopReason(StrEnum):
    ITERATION_CAP = "iteration_cap"
    NO_PROGRESS = "no_progress"


class CoordinatorPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_iterations: int = Field(default=4, ge=1, description="Coordinator invocations per task")
    parse_retries: int = Field(default=1, ge=0, description="Gateway retries on unparseable output")
    decision_attempts: int = Field(default=2, ge=1, description="Model tries before the ask-user fallback")


class CoordinatorProgress(BaseModel):
    """Persisted per task (``TaskState.coordinator``). Owned by the Coordinator node."""

    model_config = ConfigDict(frozen=True)

    iterations: int = 0
    seen_hashes: tuple[str, ...] = ()  # "<trigger>:<progress_hash>"
    last_action: Action | None = None
    stop_reason: StopReason | None = None


class CoordinatorEvent(BaseModel):
    """What the Router tells the Coordinator, beyond the task state itself."""

    model_config = ConfigDict(frozen=True)

    trigger: Trigger
    alternatives: tuple[str, ...] = Field(default=(), description="Next-ranked candidate ids, best first")
    can_widen_search: bool = False
    payment_committed: bool = False
    days_late: int | None = Field(default=None, ge=0)


class ConstraintView(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str
    field_name: str
    operator: str
    strength: ConstraintStrength


class ProposalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str
    field_name: str
    strength: ConstraintStrength
    unlocked_count: int
    cost: float


class StepView(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str
    status: str


class CoordinatorInput(BaseModel):
    """The complete prompt payload, rendered as JSON."""

    model_config = ConfigDict(frozen=True)

    trigger: Trigger
    allowed_actions: tuple[Action, ...]
    iteration: int
    constraints: tuple[ConstraintView, ...]
    proposals: tuple[ProposalView, ...]
    alternatives: tuple[str, ...]
    can_widen_search: bool
    payment_committed: bool
    days_late: int | None
    plan: tuple[StepView, ...]


class CoordinatorDecision(BaseModel):
    """The model's whole answer: a closed choice plus optional ids and a short audit reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Action
    reason: str = Field(min_length=1, max_length=300)
    proposal_id: str | None = None
    candidate_id: str | None = None
