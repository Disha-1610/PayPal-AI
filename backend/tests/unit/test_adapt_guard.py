import pytest

from app.orchestrator.guard.constraint_guard import HardConstraintViolation, assert_revision_allowed
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from app.schemas.state import merge_constraints

USER, INFERRED = ConstraintSource.USER, ConstraintSource.INFERRED


def _c(source=USER, strength=ConstraintStrength.HARD, field="budget", value=10000):
    return Constraint(field_name=field, operator=Operator.LTE, target_value=value, source=source, strength=strength)


@pytest.mark.parametrize("field", ["budget", "deadline", "vendor", "anything_else"])
def test_inferred_value_change_of_hard_constraint_is_blocked_for_any_field(field):
    hard = _c(INFERRED, field=field)  # even a hard constraint the system itself inferred is protected
    with pytest.raises(HardConstraintViolation, match="require source='user'"):
        assert_revision_allowed(hard, hard.revise(reason="guess", changed_by=INFERRED, target_value=12000))


def test_inferred_removal_and_downgrade_of_hard_constraint_are_blocked():
    hard = _c(INFERRED)
    for relaxed in (
        hard.revise(reason="drop", changed_by=INFERRED, active=False),
        hard.revise(reason="soften", changed_by=INFERRED, strength=ConstraintStrength.SOFT),
        hard.revise(reason="loosen", changed_by=INFERRED, operator=Operator.GTE),
    ):
        with pytest.raises(HardConstraintViolation):
            assert_revision_allowed(hard, relaxed)


def test_user_revision_of_hard_constraint_is_allowed():
    hard = _c()
    assert_revision_allowed(hard, hard.revise(reason="ok", changed_by=USER, target_value=12000))
    assert_revision_allowed(hard, hard.revise(reason="ok", changed_by=USER, active=False))


def test_system_may_revise_soft_inferred_constraint_but_not_override_user_statement():
    soft_inferred = _c(INFERRED, ConstraintStrength.SOFT)
    assert_revision_allowed(soft_inferred, soft_inferred.revise(reason="tune", changed_by=INFERRED, target_value=5))
    soft_user = _c(USER, ConstraintStrength.SOFT)
    with pytest.raises(HardConstraintViolation, match="user constraint"):
        assert_revision_allowed(soft_user, soft_user.revise(reason="x", changed_by=INFERRED, target_value=5))


def test_state_reducer_enforces_the_guard_and_is_a_value_error():
    hard = _c()
    sneaky = hard.revise(reason="silent relax", changed_by=INFERRED, target_value=99999)
    with pytest.raises(ValueError):
        merge_constraints([hard], [sneaky])
    assert merge_constraints([hard], [hard.revise(reason="ok", changed_by=USER, target_value=12000)])[0].target_value == 12000
