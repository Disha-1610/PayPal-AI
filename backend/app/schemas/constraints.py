"""Constraint models: what the user *wants* (preferences / limits on candidates).

Constraints are deliberately separate from authorization (what the agent is allowed
to do) and approval (what the user confirmed). See ``schemas.authorization``.
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ScalarValue = bool | int | Decimal | str
ConstraintValue = ScalarValue | list[ScalarValue]


class Operator(StrEnum):
    EQ = "=="
    NE = "!="
    IN = "in"
    NOT_IN = "not_in"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


OPERATOR_WORDS: dict[Operator, str] = {
    Operator.EQ: "equal to", Operator.NE: "different from", Operator.IN: "one of", Operator.NOT_IN: "none of",
    Operator.GT: "greater than", Operator.GTE: "greater than or equal to", Operator.LT: "less than",
    Operator.LTE: "less than or equal to",
}


def describe_constraint(field_name: str, operator: Operator, target: object) -> str:
    """A requirement in plain words for the user, e.g. ``price ceiling must be less than or equal to 40000``."""
    return f"{field_name.replace('_', ' ')} must be {OPERATOR_WORDS[operator]} {target}"


RANGE_OPERATORS = frozenset({Operator.GT, Operator.GTE, Operator.LT, Operator.LTE})
MEMBERSHIP_OPERATORS = frozenset({Operator.IN, Operator.NOT_IN})


class ConstraintSource(StrEnum):
    USER = "user"
    INFERRED = "inferred"


class ConstraintStrength(StrEnum):
    HARD = "hard"
    SOFT = "soft"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_number(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("range operators require a numeric target_value, got bool")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"range operators require a numeric target_value, got {value!r}") from exc


def _validate_value(operator: Operator, value: ConstraintValue) -> ConstraintValue:
    if operator in RANGE_OPERATORS:
        if isinstance(value, list):
            raise ValueError("range operators require a scalar target_value")
        return _coerce_number(value)
    if operator in MEMBERSHIP_OPERATORS:
        if not isinstance(value, list) or not value:
            raise ValueError("in/not_in operators require a non-empty list target_value")
        return value
    if isinstance(value, list):
        raise ValueError("==/!= operators require a scalar target_value")
    return value


class ConstraintRevision(BaseModel):
    """Immutable record of one change to a constraint (the state *before* the change)."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(ge=1, description="Revision number of the constraint that was replaced")
    operator: Operator
    target_value: ConstraintValue
    strength: ConstraintStrength
    source: ConstraintSource = Field(description="Source of the replaced revision")
    changed_by: ConstraintSource = Field(description="Source that made the change")
    reason: str
    changed_at: datetime = Field(default_factory=_utcnow)


class Constraint(BaseModel):
    model_config = ConfigDict(frozen=True)

    constraint_id: str = Field(default_factory=lambda: uuid4().hex)
    field_name: str = Field(min_length=1)
    operator: Operator
    target_value: ConstraintValue
    source: ConstraintSource = Field(description="Source of the current revision")
    strength: ConstraintStrength = ConstraintStrength.HARD
    active: bool = True
    revision: int = Field(default=1, ge=1)
    history: tuple[ConstraintRevision, ...] = ()

    @model_validator(mode="after")
    def _check_value(self) -> Self:
        # Frozen model: normalise in place (runs once at construction).
        object.__setattr__(self, "target_value", _validate_value(self.operator, self.target_value))
        return self

    def revise(
        self,
        *,
        reason: str,
        changed_by: ConstraintSource,
        operator: Operator | None = None,
        target_value: ConstraintValue | None = None,
        strength: ConstraintStrength | None = None,
        active: bool | None = None,
    ) -> "Constraint":
        """Return the next revision, appending the replaced state to ``history``."""
        record = ConstraintRevision(
            revision=self.revision,
            operator=self.operator,
            target_value=self.target_value,
            strength=self.strength,
            source=self.source,
            changed_by=changed_by,
            reason=reason,
        )
        return Constraint(
            constraint_id=self.constraint_id,
            field_name=self.field_name,
            operator=operator or self.operator,
            target_value=self.target_value if target_value is None else target_value,
            source=changed_by,
            strength=strength or self.strength,
            active=self.active if active is None else active,
            revision=self.revision + 1,
            history=(*self.history, record),
        )

    def content(self) -> tuple[object, ...]:
        """Fields that define the constraint's meaning (excludes revision bookkeeping)."""
        return (self.field_name, self.operator, self.target_value, self.strength, self.active)


class ConstraintChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    before: Constraint
    after: Constraint


class ConstraintDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    added_constraints: tuple[Constraint, ...] = ()
    removed_constraints: tuple[Constraint, ...] = ()
    modified_constraints: tuple[ConstraintChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added_constraints or self.removed_constraints or self.modified_constraints)


def diff_constraints(old: list[Constraint], new: list[Constraint]) -> ConstraintDiff:
    """Diff two constraint sets by ``constraint_id``.

    A constraint still present but flipped to ``active=False`` is reported as removed.
    """
    old_by_id = {c.constraint_id: c for c in old}
    new_by_id = {c.constraint_id: c for c in new}

    added = [c for cid, c in new_by_id.items() if cid not in old_by_id and c.active]
    removed = [
        c for cid, c in old_by_id.items() if c.active and (cid not in new_by_id or not new_by_id[cid].active)
    ]
    modified = [
        ConstraintChange(before=old_by_id[cid], after=c)
        for cid, c in new_by_id.items()
        if cid in old_by_id and old_by_id[cid].active and c.active and old_by_id[cid].content() != c.content()
    ]
    return ConstraintDiff(
        added_constraints=tuple(added),
        removed_constraints=tuple(removed),
        modified_constraints=tuple(modified),
    )
