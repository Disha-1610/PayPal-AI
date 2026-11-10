"""Deadline checking: compare an event's new date with the user's ORIGINAL constraint.

The deadline always comes from the persisted constraints in ``TaskView``, never from the event or an LLM.
This module only detects risk; it never edits a constraint. Changing a deadline is a user decision (Phase 4).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domains.base import EventRule
from app.schemas.constraints import ConstraintStrength, Operator
from app.schemas.monitor import MonitorEvent, Problem, TaskView

_UPPER_BOUND = frozenset({Operator.LTE, Operator.LT, Operator.EQ})


class InvalidEvent(ValueError):
    """The event cannot be interpreted (missing or malformed required field). It is rejected, never guessed."""


@dataclass(frozen=True)
class ResolvedDeadline:
    constraint_id: str
    at: datetime


@dataclass(frozen=True)
class Assessment:
    problem: Problem | None = None
    constraint_id: str | None = None
    deadline: datetime | None = None
    new_date: datetime | None = None


def parse_instant(value: object) -> datetime:
    """ISO 8601 -> aware UTC datetime. Raises ``InvalidEvent`` for anything else."""
    if not isinstance(value, str):
        raise InvalidEvent("date must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidEvent("date is not valid ISO 8601") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def resolve_deadline(view: TaskView, field_name: str) -> ResolvedDeadline | None:
    """Active hard upper-bound constraint on ``field_name`` as an absolute instant, or ``None`` if unknown.

    A number means "days from ``anchor_at``" (e.g. ``delivery_days``); a string must be an ISO date.
    """
    for constraint in view.constraints:
        if not (
            constraint.active
            and constraint.strength is ConstraintStrength.HARD
            and constraint.field_name == field_name
            and constraint.operator in _UPPER_BOUND
        ):
            continue
        value = constraint.target_value
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            if view.anchor_at is None:
                return None
            return ResolvedDeadline(constraint.constraint_id, view.anchor_at + timedelta(days=float(value)))
        if isinstance(value, str):
            try:
                return ResolvedDeadline(constraint.constraint_id, parse_instant(value))
            except InvalidEvent:
                return None
    return None


def assess_event(rule: EventRule, event: MonitorEvent, view: TaskView) -> Assessment:
    """Decide whether ``event`` raises its rule's problem.

    * No problem on the rule: nothing to assess.
    * Problem without a date check (``deadline_field``/``date_key`` unset): raised unconditionally.
    * Date-checked: raised only when the new date is later than the original deadline. A task with no
      deadline constraint cannot be late, so a delay there is not a problem.
    """
    if rule.problem is None:
        return Assessment()
    if rule.deadline_field is None or rule.date_key is None:
        deadline = resolve_deadline(view, rule.deadline_field) if rule.deadline_field else None
        return Assessment(
            problem=rule.problem,
            constraint_id=deadline.constraint_id if deadline else None,
            deadline=deadline.at if deadline else None,
        )
    new_date = parse_instant(event.payload.get(rule.date_key))
    deadline = resolve_deadline(view, rule.deadline_field)
    if deadline is None or new_date <= deadline.at:
        return Assessment(new_date=new_date, deadline=deadline.at if deadline else None)
    return Assessment(
        problem=rule.problem, constraint_id=deadline.constraint_id, deadline=deadline.at, new_date=new_date
    )
