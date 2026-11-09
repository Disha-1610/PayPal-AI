"""Domain plug-in contract.

A plug-in is *data*: which fields Intent may extract, which of them block progress when
missing, and the plan template. Domain knowledge lives here, never in the Intent prompt.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.plan import Plan, PlanStep


class ValueType(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    BOOLEAN = "boolean"
    LIST = "list"


class FieldSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    value_type: ValueType
    description: str
    blocking: bool = False
    question: str | None = Field(default=None, description="Asked verbatim when a blocking field is missing")


class DomainPlugin(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    goal_question: str = Field(description="Asked when no goal could be extracted")
    fields: tuple[FieldSpec, ...]
    plan_steps: tuple[PlanStep, ...]

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def blocking_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.blocking)

    def plan_template(self) -> Plan:
        return Plan(steps=self.plan_steps)
