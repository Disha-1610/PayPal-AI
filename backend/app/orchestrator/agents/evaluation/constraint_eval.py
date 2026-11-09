"""Shared constraint evaluation for the Phase 3 steps: resolve a candidate value, test it against a constraint."""

from decimal import Decimal, InvalidOperation

from app.schemas.candidate import Candidate, DataQuality
from app.schemas.constraints import MEMBERSHIP_OPERATORS, RANGE_OPERATORS, Constraint, Operator

# Constraint field name -> Candidate core field, where the two differ.
_CORE_ALIASES = {"budget": "total_cost"}
_CORE_FIELDS = frozenset({"total_cost", "rating", "review_count", "complaint_count", "return_window_days"})
_UNUSABLE = frozenset({DataQuality.MISSING, DataQuality.UNRELIABLE})

Comparable = bool | Decimal | str | list[str]


class Unverifiable(Exception):
    """Raised inside the evaluator when a constraint cannot be checked against a candidate."""


def resolve_value(candidate: Candidate, field_name: str) -> object:
    core = _CORE_ALIASES.get(field_name, field_name)
    if core in _CORE_FIELDS:
        name, value = core, getattr(candidate, core)
    else:
        name, value = field_name, candidate.attributes.get(field_name)
    if value is None or candidate.quality.get(name) in _UNUSABLE:
        raise Unverifiable(f"{field_name} is missing or unreliable")
    return value


def to_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise Unverifiable("expected a number, got a boolean")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise Unverifiable(f"expected a number, got {value!r}") from exc
    if not number.is_finite():
        raise Unverifiable(f"expected a finite number, got {value!r}")
    return number


def _norm(value: object) -> Comparable:
    """Normalise to a comparable form: numbers -> Decimal, text -> casefolded, lists -> casefolded lists."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return to_decimal(value)
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]  # type: ignore[misc]
    raise Unverifiable(f"unsupported value type {type(value).__name__}")


def satisfies(constraint: Constraint, actual: object) -> bool:
    op, target = constraint.operator, constraint.target_value
    if op in RANGE_OPERATORS:
        left, right = to_decimal(actual), to_decimal(target)
        return {
            Operator.GT: left > right,
            Operator.GTE: left >= right,
            Operator.LT: left < right,
            Operator.LTE: left <= right,
        }[op]

    value = _norm(actual)
    if op in MEMBERSHIP_OPERATORS:
        wanted = _norm(target)
        assert isinstance(wanted, list)
        # A list-valued attribute (e.g. skills) must contain every wanted item for ``in``
        # and none of them for ``not_in``; a scalar attribute is tested for membership.
        if isinstance(value, list):
            present = [w in value for w in wanted]
            return all(present) if op is Operator.IN else not any(present)
        return (value in wanted) if op is Operator.IN else (value not in wanted)

    wanted = _norm(target)
    if isinstance(value, list) or isinstance(wanted, list):
        raise Unverifiable("cannot compare a list with ==/!=")
    equal = value == wanted
    return equal if op is Operator.EQ else not equal
