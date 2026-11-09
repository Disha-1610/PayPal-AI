"""Adapt (Phase 4) models. Produced by deterministic code only; the LLM never writes any of these.

Domain-agnostic: a proposal edits a ``Constraint`` (field, operator, value), so it works the same
for a price cap, a deadline, a skill list or a vendor lock in any domain plug-in.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.constraints import ConstraintValue, Operator


class RecoveryStep(StrEnum):
    """Next rung of the recovery ladder (4.2), executed by the discovery node or the graph."""

    LOCAL_RECOVERY = "local_recovery"  # broaden / vary the search strictly inside the user's source lock
    CROSS_DOMAIN = "cross_domain"  # other vendors / platforms; only when nothing is locked
    REFRESH_POOL = "refresh_pool"  # the cached near-miss pool is stale: re-run discovery once, unchanged
    ASK_USER = "ask_user"  # pause with ``interrupt()`` and propose a relaxation
    STOP = "stop"


class TerminationReason(StrEnum):
    ATTEMPT_CAP = "attempt_cap"
    EXHAUSTED = "exhausted"  # every permitted source / alternative was tried and nothing can be proposed
    CANCELLED = "cancelled"
    NO_PROGRESS = "no_progress"
    USER_DECLINED = "user_declined"
    PROMPT_CAP = "prompt_cap"


class RelaxationOp(StrEnum):
    UPDATE = "update"
    REMOVE = "remove"


class RelaxationProposal(BaseModel):
    """The smallest edit of one hard constraint that admits ``unlocked_ids`` (4.3)."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    constraint_id: str
    constraint_revision: int = Field(ge=1, description="Revision the proposal was computed against")
    field_name: str
    op: RelaxationOp
    operator: Operator | None = None
    new_value: ConstraintValue | None = None
    unlocked_ids: tuple[str, ...]
    cost: float = Field(ge=0, description="Normalised size of the change; lower is a smaller ask")
    description: str

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.op is RelaxationOp.UPDATE and (self.operator is None or self.new_value is None):
            raise ValueError("an update proposal needs operator and new_value")
        return self


class AdaptPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=6, ge=1, description="Total adapt evaluations before stopping")
    max_local_attempts: int = Field(default=2, ge=0)
    max_cross_domain_attempts: int = Field(default=1, ge=0)
    max_user_prompts: int = Field(default=2, ge=1)
    max_refreshes: int = Field(default=1, ge=0)
    max_proposals: int = Field(default=3, ge=1)
    max_pool_age_seconds: int = Field(default=900, ge=1, description="Older cached candidates are re-fetched first")


class AdaptProgress(BaseModel):
    """Persisted ladder position for one task (``TaskState.adapt``). Owned by the adapt node."""

    model_config = ConfigDict(frozen=True)

    attempts: int = 0
    local_attempts: int = 0
    cross_domain_attempts: int = 0
    refreshes: int = 0
    prompts: int = 0
    local_exhausted: bool = False
    cross_domain_exhausted: bool = False
    seen_hashes: tuple[str, ...] = ()
    next_step: RecoveryStep | None = None
    termination: TerminationReason | None = None
    detail: str = ""


class UserDecisionKind(StrEnum):
    ACCEPT = "accept"  # take one of the proposals
    DECLINE = "decline"  # none of them
    CANCEL = "cancel"  # abandon the task
    REPLY = "reply"  # free text; interpreted by the Intent agent as a constraint patch


class UserDecision(BaseModel):
    """Payload the user's reply to ``interrupt()`` is validated into."""

    model_config = ConfigDict(frozen=True)

    kind: UserDecisionKind
    proposal_id: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.kind is UserDecisionKind.ACCEPT and not self.proposal_id:
            raise ValueError("accept requires proposal_id")
        if self.kind is UserDecisionKind.REPLY and not (self.text and self.text.strip()):
            raise ValueError("reply requires text")
        return self
