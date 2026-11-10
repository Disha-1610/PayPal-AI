"""Step 1: intent constraints + domain plug-in -> ``SearchRequest``.

The query text is the goal plus the user's *soft* text preferences (what ladder rung 2 may drop).
Filters come only from active hard ``==`` constraints on plug-in fields, so they never change
when a query is rewritten.
"""

from collections.abc import Iterable
from decimal import Decimal

from app.connectors.base import SearchRequest
from app.domains.base import DomainPlugin, ValueType
from app.schemas.candidate import AttributeValue
from app.schemas.constraints import Constraint, ConstraintStrength, Operator
from app.schemas.discovery import QueryPlan


def soft_terms(constraints: Iterable[Constraint], plugin: DomainPlugin) -> tuple[str, ...]:
    """Text values of active soft ``==`` / ``in`` constraints, in the order the user stated them."""
    terms: dict[str, None] = {}
    for c in constraints:
        spec = plugin.field(c.field_name)
        if not (c.active and c.strength is ConstraintStrength.SOFT and spec and spec.value_type is ValueType.TEXT):
            continue
        if c.operator in (Operator.EQ, Operator.IN):
            values = c.target_value if isinstance(c.target_value, list) else [c.target_value]
            terms.update(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))
    return tuple(terms)


def initial_plan(goal: str, constraints: Iterable[Constraint], plugin: DomainPlugin) -> QueryPlan:
    if not goal.strip():
        raise ValueError("cannot build a search request without a goal")
    return QueryPlan(core=" ".join(goal.split()), soft_terms=soft_terms(constraints, plugin))


def _filter_value(value: object) -> AttributeValue | None:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def hard_filters(constraints: Iterable[Constraint], plugin: DomainPlugin) -> dict[str, AttributeValue]:
    filters: dict[str, AttributeValue] = {}
    for c in constraints:
        if c.active and c.strength is ConstraintStrength.HARD and c.operator is Operator.EQ and plugin.field(c.field_name):
            value = _filter_value(c.target_value)
            if value is not None:
                filters[c.field_name] = value
    return filters


def build_search_request(
    plan: QueryPlan, constraints: Iterable[Constraint], plugin: DomainPlugin, *, limit: int = 10
) -> SearchRequest:
    return SearchRequest(query=plan.text, domain=plugin.name, filters=hard_filters(constraints, plugin), limit=limit)
