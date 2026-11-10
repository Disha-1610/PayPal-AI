from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.authorization import Action, Approval, TaskAuthorization, evaluate_authorization
from app.schemas.constraints import (
    Constraint,
    ConstraintSource,
    ConstraintStrength,
    Operator,
    diff_constraints,
)


def _price_limit() -> Constraint:
    return Constraint(field_name="price", operator=Operator.LT, target_value=5000, source=ConstraintSource.USER)


def test_range_operator_coerces_and_validates():
    assert _price_limit().target_value == Decimal(5000)
    assert Constraint(field_name="p", operator=Operator.LT, target_value="4999.5", source="user").target_value == Decimal("4999.5")
    with pytest.raises(ValidationError):
        Constraint(field_name="p", operator=Operator.GT, target_value="cheap", source="user")
    with pytest.raises(ValidationError):
        Constraint(field_name="p", operator=Operator.IN, target_value=[], source="user")
    with pytest.raises(ValidationError):
        Constraint(field_name="p", operator=Operator.EQ, target_value=["a"], source="user")


def test_revise_keeps_source_and_history():
    c1 = _price_limit()
    c2 = c1.revise(reason="user raised budget", changed_by=ConstraintSource.USER, target_value=6000)
    c3 = c2.revise(reason="relaxed", changed_by=ConstraintSource.INFERRED, strength=ConstraintStrength.SOFT)
    assert (c3.revision, c3.source, c3.strength) == (3, ConstraintSource.INFERRED, ConstraintStrength.SOFT)
    assert [h.revision for h in c3.history] == [1, 2]
    assert c3.history[0].target_value == Decimal(5000)
    assert c3.history[1].changed_by is ConstraintSource.INFERRED
    assert c1.revision == 1 and c1.history == ()  # immutable


def test_diff_added_removed_modified():
    keep = _price_limit()
    gone = Constraint(field_name="brand", operator=Operator.IN, target_value=["a"], source="user")
    new = Constraint(field_name="color", operator=Operator.EQ, target_value="red", source="inferred")
    changed = keep.revise(reason="r", changed_by=ConstraintSource.USER, target_value=4000)
    diff = diff_constraints([keep, gone], [changed, new])
    assert [c.field_name for c in diff.added_constraints] == ["color"]
    assert [c.field_name for c in diff.removed_constraints] == ["brand"]
    assert diff.modified_constraints[0].before.target_value == Decimal(5000)
    assert diff.modified_constraints[0].after.target_value == Decimal(4000)
    assert diff_constraints([keep], [keep]).is_empty


def test_deactivated_constraint_reported_removed():
    c = _price_limit()
    off = c.revise(reason="drop", changed_by=ConstraintSource.USER, active=False)
    assert diff_constraints([c], [off]).removed_constraints == (c,)


def _auth(**kw) -> TaskAuthorization:
    base = dict(
        run_id="r1",
        max_spend=Decimal(5000),
        permitted_platforms=frozenset({"shop"}),
        permitted_actions=frozenset({Action.CREATE_ORDER, Action.CAPTURE_PAYMENT}),
    )
    return TaskAuthorization(**{**base, **kw})


def _decide(auth, action=Action.CREATE_ORDER, amount=Decimal(4500), platform="shop", approvals=None, verifier=None):
    return evaluate_authorization(
        auth, action=action, platform=platform, amount=amount, currency="INR", approvals=approvals,
        approval_verifier=verifier,
    )


def test_money_actions_require_approval_by_default():
    auth = _auth()
    for action in (Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT):
        assert action in auth.approval_required_actions
    assert _decide(auth).requires_approval


def test_authorization_denies_by_default_and_checks_limits():
    auth = _auth(approval_required_actions=frozenset())
    assert _decide(auth).allowed
    assert not _decide(auth, amount=Decimal(5001)).allowed
    assert not _decide(auth, platform="other").allowed
    assert not _decide(auth, action=Action.VOID_PAYMENT).allowed
    assert not _decide(_auth(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))).allowed


def test_approval_is_separate_from_authorization():
    auth = _auth()
    denied = _decide(auth, Action.CAPTURE_PAYMENT)
    assert not denied.allowed and denied.requires_approval

    def approval(amount):
        return Approval(
            run_id="r1", authorization_id=auth.authorization_id, action=Action.CAPTURE_PAYMENT,
            candidate_id="shoe-1", platform="shop", amount=amount, currency="INR", approved_by="u1",
        )

    assert _decide(auth, Action.CAPTURE_PAYMENT, approvals=[approval(Decimal(4500))], verifier=lambda a: True).allowed
    # approval for a different amount does not cover this capture
    assert not _decide(auth, Action.CAPTURE_PAYMENT, approvals=[approval(Decimal(4000))], verifier=lambda a: True).allowed


def test_unverified_approval_never_unlocks_an_approval_required_action():
    auth = _auth()
    forged = Approval(
        run_id="r1", authorization_id=auth.authorization_id, action=Action.CREATE_ORDER, candidate_id="shoe-1",
        platform="shop", amount=Decimal(4500), currency="INR", approved_by="u1",
    )
    no_verifier = _decide(auth, approvals=[forged])
    assert not no_verifier.allowed and no_verifier.requires_approval
    rejected = _decide(auth, approvals=[forged], verifier=lambda a: False)
    assert not rejected.allowed and rejected.requires_approval
