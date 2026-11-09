from decimal import Decimal

from app.orchestrator.progress.hashing import progress_hash
from app.schemas.constraints import Constraint, ConstraintSource, Operator


def _c(field, op, value, **kw):
    return Constraint(field_name=field, operator=op, target_value=value, source=ConstraintSource.USER, **kw)


A, B = _c("budget", Operator.LTE, 10000), _c("airline", Operator.IN, ["air india", "vistara"])


def test_hash_is_order_independent_and_stable():
    h = progress_hash(["x", "y"], [A, B])
    assert h == progress_hash(["y", "x", "x"], [B, A])
    assert len(h) == 64 and h == progress_hash(["x", "y"], [A, B])


def test_numeric_and_list_representations_are_canonical():
    same = _c("budget", Operator.LTE, Decimal("10000.00"))
    assert progress_hash(["x"], [A]) == progress_hash(["x"], [same])
    reordered = _c("airline", Operator.IN, ["vistara", "air india"])
    assert progress_hash([], [B]) == progress_hash([], [reordered])


def test_hash_changes_with_candidates_or_active_constraints():
    base = progress_hash(["x"], [A])
    assert base != progress_hash(["x", "y"], [A])
    assert base != progress_hash(["x"], [_c("budget", Operator.LTE, 12000)])


def test_inactive_constraint_is_ignored_and_relaxation_changes_hash():
    off = A.revise(reason="r", changed_by=ConstraintSource.USER, active=False)
    assert progress_hash(["x"], [off, B]) == progress_hash(["x"], [B])
    assert progress_hash(["x"], [off]) != progress_hash(["x"], [A])
