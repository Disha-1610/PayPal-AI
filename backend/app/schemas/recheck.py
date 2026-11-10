"""What a pre-execution recheck found, expressed domain-independently.

``ObservedTerms`` is the live counterpart of ``ApprovalTerms``: what the connector reports *now*.
``ApprovalTerms`` holds ceilings and deadlines; ``ObservedTerms`` holds actual totals and estimates.
"""

from datetime import datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.approval_terms import Scalar


class ObservedTerms(BaseModel):
    model_config = ConfigDict(frozen=True)

    available: bool | None = Field(default=None, description="None = connector could not tell")
    subject_id: str
    subject_attributes: dict[str, Scalar] = Field(default_factory=dict)
    parties: dict[str, str] = Field(default_factory=dict)
    quantity: int = Field(default=1, ge=1)
    currency: str = Field(min_length=3, max_length=3)
    total_minor: int | None = Field(default=None, ge=0, description="None = price unknown")
    available_by: datetime | None = Field(default=None, description="Estimated delivery/completion")
    permissions: frozenset[str] = frozenset()  # permissions the action would need now
    constraints: dict[str, Scalar] = Field(default_factory=dict)
    domain_data: dict[str, Scalar] = Field(default_factory=dict)


    @field_validator("available_by")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        """Naive datetimes cannot be compared with a deadline; producers must normalise (or pass None)."""
        if value is not None and value.tzinfo is None:
            raise ValueError("available_by must be timezone-aware")
        return value


class MismatchAction(IntEnum):
    """Ordered by severity; the most severe change decides the outcome."""

    PROCEED = 0
    INSUFFICIENT_DATA = 1  # cannot confirm -> refresh or ask, never guess
    DEADLINE_MISSED = 2  # route to Adapt
    REAPPROVE = 3  # block and ask the user again with a diff
    UNAVAILABLE = 4  # route back to Evaluator / Discovery


class ChangeKind(StrEnum):
    UNAVAILABLE = "unavailable"
    PRICE_INCREASE = "price_increase"
    PRICE_DECREASE = "price_decrease"
    SUSPICIOUS_DROP = "suspicious_drop"
    CURRENCY_CHANGED = "currency_changed"
    DEADLINE_MISSED = "deadline_missed"
    DEADLINE_UNKNOWN = "deadline_unknown"
    PRICE_UNKNOWN = "price_unknown"
    AVAILABILITY_UNKNOWN = "availability_unknown"
    FIELD_CHANGED = "field_changed"
    FIELD_UNKNOWN = "field_unknown"
    SCOPE_INCREASED = "scope_increased"


class TermChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    kind: ChangeKind
    action: MismatchAction
    approved: str | None = None
    observed: str | None = None


class MismatchReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: MismatchAction
    changes: tuple[TermChange, ...] = ()

    @property
    def can_proceed(self) -> bool:
        return self.action is MismatchAction.PROCEED
