"""Phase 7 (Monitor and Close) models, shared by every domain.

The Monitor detects *what happened*; the Router (``orchestrator.router.monitor``) decides *what runs next*.
Nothing here carries free text that an LLM would read: external payloads are stored for audit only and
are never interpreted beyond the single field a domain rule names.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.constraints import Constraint


class EventOutcome(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"  # same (source, event_id) already processed
    STALE = "stale"  # older than the entity's latest applied event
    REJECTED = "rejected"  # unknown task/type, bad payload, wrong domain, future-dated


class Problem(StrEnum):
    DEADLINE_RISK = "deadline_risk"
    NON_DELIVERY = "non_delivery"  # failed / returned / not delivered
    WORK_REJECTED = "work_rejected"  # milestone rejected, remote agent failed
    PAYMENT_FAILED = "payment_failed"  # authorization voided/expired or capture denied outside our control


class Route(StrEnum):
    CONTINUE_MONITORING = "continue_monitoring"
    ADAPT = "adapt"  # Phase 4, nothing bought yet
    REPLAN_POST_PURCHASE = "replan_post_purchase"  # money is committed: release/refund via Guard, then Phase 4
    REMEDY = "remedy"  # refund / dispute, gated by ``Action`` permissions
    ASK_USER = "ask_user"
    CLOSE = "close"


class MonitorEvent(BaseModel):
    """One normalised external event. ``(source, event_id)`` is the idempotency key."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)  # e.g. "paypal", "amazon", "timer"
    domain: str = Field(min_length=1, max_length=32)
    event_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=128)  # order, milestone, remote task or payment
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)  # untrusted; kept for audit

    @field_validator("occurred_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TaskView(BaseModel):
    """The slice of task state the Monitor needs. Loaded from the persisted state, never from an LLM."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    domain: str
    constraints: tuple[Constraint, ...] = ()
    anchor_at: datetime | None = Field(
        default=None, description="When the order/engagement started; turns a 'days' constraint into a date"
    )
    expected_entities: frozenset[str] = Field(
        default=frozenset(), description="Entities that must all finish (e.g. every milestone); empty = those observed"
    )


class MonitorResult(BaseModel):
    """Structured outcome of processing one event. Consumed by the router."""

    model_config = ConfigDict(frozen=True)

    event: MonitorEvent
    outcome: EventOutcome
    reason: str | None = None
    problem: Problem | None = None
    affected_constraint_id: str | None = None
    deadline: datetime | None = None
    new_date: datetime | None = None
    payment_committed: bool = False  # a payment is authorized or captured for this run
    completed: bool = False
    blockers: tuple[str, ...] = ()  # why the task is not complete yet (audit / user-facing status)

    @property
    def deadline_at_risk(self) -> bool:
        return self.problem is Problem.DEADLINE_RISK
