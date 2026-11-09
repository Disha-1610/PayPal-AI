"""Intent schemas.

``IntentExtraction`` / ``IntentPatch`` are what the LLM is allowed to produce. They carry no
status, no ``old`` values and no ``source``: those are computed or fixed by code so the model
cannot claim them. ``IntentResult`` is the deterministic outcome handed to the rest of the system.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.constraints import (
    Constraint,
    ConstraintSource,
    ConstraintStrength,
    ConstraintValue,
    Operator,
)

LLMScalar = bool | int | float | str
LLMValue = LLMScalar | list[LLMScalar]


class ExtractedConstraint(BaseModel):
    field: str = Field(description="Exact field name from the allowed field list")
    operator: Operator
    value: LLMValue
    strength: ConstraintStrength
    evidence: str = Field(min_length=1, description="Exact words copied from the user's message")


class IntentExtraction(BaseModel):
    goal: str = Field(description="What the user wants, in the user's own terms; empty if none stated")
    out_of_scope: bool = Field(default=False, description="True if the request is outside the active domain")
    constraints: list[ExtractedConstraint] = Field(default_factory=list)
    conflicts: list[str] = Field(
        default_factory=list, description="Requirements stated by the user that contradict each other"
    )


class PatchOp(StrEnum):
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


class IntentPatchOp(BaseModel):
    op: PatchOp
    field: str = Field(description="Exact field name from the allowed field list")
    operator: Operator | None = None
    value: LLMValue | None = None
    strength: ConstraintStrength | None = None
    evidence: str = Field(min_length=1, description="Exact words copied from the user's message")

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.op is PatchOp.ADD and (self.operator is None or self.value is None):
            raise ValueError("add requires operator and value")
        if self.op is PatchOp.UPDATE and self.operator is None and self.value is None and self.strength is None:
            raise ValueError("update requires at least one of operator, value, strength")
        return self


class IntentPatch(BaseModel):
    ops: list[IntentPatchOp] = Field(default_factory=list)


class IntentStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"
    OUT_OF_SCOPE = "out_of_scope"


class IntentResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: IntentStatus
    goal: str
    constraints: tuple[Constraint, ...]
    missing: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()


class AppliedChange(BaseModel):
    """One applied patch operation, e.g. ``{op: update, field: budget, old: 5000, new: 5500}``."""

    model_config = ConfigDict(frozen=True)

    op: PatchOp
    field: str
    old: ConstraintValue | None = None
    new: ConstraintValue | None = None
    source: ConstraintSource = ConstraintSource.USER
