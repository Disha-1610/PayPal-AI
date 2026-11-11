"""Domain plug-in contract.

A plug-in is *data*: which fields Intent may extract, which of them block progress when
missing, and the plan template. Domain knowledge lives here, never in the Intent prompt.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.monitor import Problem
from app.schemas.plan import Plan, PlanStep
from app.schemas.verification import VerificationSpec


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


class EventRule(BaseModel):
    """How one event type affects progress. Pure data: the Monitor engine interprets it."""

    model_config = ConfigDict(frozen=True)

    event_type: str = Field(min_length=1)
    rank: int = Field(ge=0, description="Tie-breaker when two events share a timestamp; later lifecycle = higher")
    problem: Problem | None = None
    deadline_field: str | None = Field(
        default=None, description="Constraint compared with the event's date; with no date_key the problem is unconditional"
    )
    date_key: str | None = Field(default=None, description="Payload key holding the new date (ISO 8601)")


# Payment events come from PayPal webhooks and mean the same in every domain.
PAYMENT_RULES = (
    EventRule(event_type="PAYMENT_CAPTURED", rank=40),
    EventRule(event_type="PAYMENT_REFUNDED", rank=50),
    EventRule(event_type="PAYMENT_AUTHORIZATION_VOIDED", rank=30, problem=Problem.PAYMENT_FAILED),
    EventRule(event_type="PAYMENT_CAPTURE_DENIED", rank=30, problem=Problem.PAYMENT_FAILED),
)


class MonitorSpec(BaseModel):
    """What the Monitor needs to know about a domain: its events, its deadline and when it is done."""

    model_config = ConfigDict(frozen=True)

    rules: tuple[EventRule, ...] = ()
    completed_event_types: frozenset[str] = frozenset()
    deadline_field: str | None = Field(
        default=None, description="Constraint the timer checks; when passed with work unfinished, DEADLINE_PASSED is raised"
    )

    def rule(self, event_type: str) -> EventRule | None:
        return next((r for r in (*self.rules, *PAYMENT_RULES) if r.event_type == event_type), None)


class DomainPlugin(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    goal_question: str = Field(description="Asked when no goal could be extracted")
    fields: tuple[FieldSpec, ...]
    plan_steps: tuple[PlanStep, ...]
    source_fields: tuple[str, ...] = Field(
        default=(),
        description="Fields that lock WHERE results may come from (vendor, platform, method). "
        "A hard constraint on one of them forbids cross-domain expansion in Phase 4.",
    )

    negotiates: bool = Field(
        default=False, description="A failed verification opens a negotiation (contractor / A2A) instead of a new search"
    )

    verification: VerificationSpec = Field(
        default_factory=VerificationSpec, description="How Phase 5 verifies delivery before settlement"
    )

    monitor: MonitorSpec = Field(default_factory=MonitorSpec, description="How Phase 7 reads this domain's events")

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def blocking_fields(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in self.fields if f.blocking)

    def plan_template(self) -> Plan:
        return Plan(steps=self.plan_steps)
