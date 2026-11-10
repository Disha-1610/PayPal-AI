from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.orchestrator.guard.approval_guard import (
    ApprovalError,
    ApprovalStatus,
    approval_keys,
    issue_approval,
    signed_approval_verifier,
    verify_approval,
)
from app.schemas.approval_terms import ApprovalTerms, from_minor_units, to_minor_units
from app.schemas.authorization import (
    Action,
    Approval,
    TaskAuthorization,
    evaluate_authorization,
)

KEY = b"k" * 32
KEYS = {"v1": KEY}
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DEADLINE = datetime(2026, 11, 20, tzinfo=UTC)


def terms(**kw) -> ApprovalTerms:
    base = dict(
        subject_id="shoe-1",
        subject_attributes={"size": "M", "color": "Black"},
        parties={"buyer_id": "u1", "payee_id": "merchant-1"},
        currency="INR",
        max_total_minor=550000,
        latest_acceptable_at=DEADLINE,
        permissions=frozenset({"purchase"}),
    )
    return ApprovalTerms(**{**base, **kw})


def issue(t: ApprovalTerms | None = None, **kw) -> Approval:
    args = dict(
        terms=t or terms(), run_id="r1", authorization_id="a1", action=Action.CREATE_ORDER, candidate_id="shoe-1",
        platform="shop", approved_by="u1", session_id="s1", key=KEY, key_id="v1", ttl=timedelta(minutes=10), now=NOW,
    )
    return issue_approval(**{**args, **kw})


def verify(approval: Approval, current: ApprovalTerms | None = None, **kw):
    args = dict(
        current_terms=current or terms(), keys=KEYS, run_id="r1", session_id="s1", approved_by="u1",
        action=Action.CREATE_ORDER, now=NOW + timedelta(minutes=1),
    )
    return verify_approval(approval, **{**args, **kw})


def test_minor_units_are_exact_integers():
    assert to_minor_units(Decimal("4999.995"), "INR") == 500000
    assert to_minor_units(Decimal(500), "JPY") == 500
    assert from_minor_units(550000, "INR") == Decimal(5500)


def test_hash_is_order_insensitive_and_ignores_display_only():
    a = terms(subject_attributes={"size": "M", "color": "Black"}, display={"title": "Nike"})
    b = terms(subject_attributes={"color": "Black", "size": "M"}, display={"title": "Different title"})
    assert a.terms_hash() == b.terms_hash()


@pytest.mark.parametrize(
    "change",
    [
        {"subject_attributes": {"size": "L", "color": "Black"}},
        {"parties": {"buyer_id": "u1", "payee_id": "merchant-2"}},
        {"max_total_minor": 550001},
        {"quantity": 2},
        {"currency": "USD"},
        {"latest_acceptable_at": DEADLINE + timedelta(days=1)},
        {"permissions": frozenset({"purchase", "refund"})},
        {"domain_data": {"gift": True}},
    ],
)
def test_every_material_field_changes_the_hash(change):
    assert terms(**change).terms_hash() != terms().terms_hash()


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        terms(latest_acceptable_at=datetime(2026, 11, 20)).canonical()


def test_valid_approval_verifies():
    assert verify(issue()).valid


def test_changed_terms_invalidate_approval():
    verdict = verify(issue(), terms(max_total_minor=600000))
    assert verdict.status is ApprovalStatus.TERMS_CHANGED


def test_tampered_token_or_terms_fail_signature():
    approval = issue()
    assert verify(approval.model_copy(update={"signature": "0" * 64})).status is ApprovalStatus.BAD_SIGNATURE
    # attacker swaps stored terms_hash to match inflated terms: signature no longer matches
    inflated = terms(max_total_minor=900000)
    forged = approval.model_copy(update={"terms_hash": inflated.terms_hash()})
    assert verify(forged, inflated).status is ApprovalStatus.BAD_SIGNATURE
    # attacker raises the amount ceiling on the approval
    assert verify(approval.model_copy(update={"max_amount": Decimal(99999)})).status is ApprovalStatus.BAD_SIGNATURE


def test_expiry_unknown_key_and_unsigned():
    approval = issue()
    assert verify(approval, now=NOW + timedelta(minutes=10)).status is ApprovalStatus.EXPIRED
    assert verify(approval, keys={"v2": KEY}).status is ApprovalStatus.UNKNOWN_KEY
    plain = Approval(
        run_id="r1", authorization_id="a1", action=Action.CREATE_ORDER, candidate_id="c", platform="shop",
        amount=Decimal(1), currency="INR", approved_by="u1",
    )
    assert verify(plain).status is ApprovalStatus.UNSIGNED


@pytest.mark.parametrize(
    "override",
    [{"run_id": "r2"}, {"session_id": "s2"}, {"approved_by": "u2"}, {"action": Action.CAPTURE_PAYMENT}],
)
def test_approval_is_bound_to_run_user_session_and_action(override):
    assert verify(issue(), **override).status is ApprovalStatus.WRONG_SUBJECT


def test_wrong_key_signature_rejected():
    assert verify(issue(key=b"x" * 32)).status is ApprovalStatus.BAD_SIGNATURE


def test_issue_requires_key():
    with pytest.raises(ApprovalError):
        issue(key=b"")


def test_max_total_approval_covers_lower_price_but_not_higher():
    approval = issue()  # ceiling 5500
    auth = TaskAuthorization(
        authorization_id="a1", run_id="r1", max_spend=Decimal(6000), permitted_platforms=frozenset({"shop"}),
        permitted_actions=frozenset({Action.CREATE_ORDER}),
    )

    verifier = signed_approval_verifier(
        current_terms=terms(), keys=KEYS, run_id="r1", session_id="s1", approved_by="u1", now=NOW + timedelta(minutes=1)
    )

    def decide(amount, now=NOW):
        return evaluate_authorization(
            auth, action=Action.CREATE_ORDER, platform="shop", amount=Decimal(amount), currency="INR",
            approvals=[approval], approval_verifier=verifier, now=now,
        )

    assert decide(5100).allowed
    assert decide(5500).allowed
    assert decide(5501).requires_approval
    assert decide(5100, now=NOW + timedelta(minutes=11)).requires_approval  # expired


def test_stored_terms_must_match_the_signed_hash():
    approval = issue()
    swapped = approval.model_copy(update={"terms": terms(max_total_minor=900000)})
    verdict = verify(swapped)
    assert verdict.status is ApprovalStatus.BAD_SIGNATURE and "stored terms" in verdict.detail
    assert verify(approval.model_copy(update={"terms": None})).status is ApprovalStatus.BAD_SIGNATURE


def test_signature_survives_decimal_scale_changes():
    approval = issue()
    rescaled = approval.model_copy(update={"max_amount": Decimal("5500.00"), "amount": Decimal("5500.000")})
    assert verify(rescaled).valid


def test_signed_verifier_rejects_unsigned_and_tampered_approvals():
    verifier = signed_approval_verifier(
        current_terms=terms(), keys=KEYS, run_id="r1", session_id="s1", approved_by="u1", now=NOW + timedelta(minutes=1)
    )
    approval = issue()
    assert verifier(approval)
    assert not verifier(approval.model_copy(update={"signature": "0" * 64}))
    unsigned = approval.model_copy(update={"signature": None})
    assert not verifier(unsigned)


def test_keys_come_from_settings_and_fail_closed():
    from pydantic import SecretStr

    from app.core.config import Settings

    assert approval_keys(Settings(approval_signing_key=SecretStr("abc"), approval_key_id="v9")) == {"v9": b"abc"}
    with pytest.raises(ApprovalError):
        approval_keys(Settings(approval_signing_key=None))
    with pytest.raises(ApprovalError):
        approval_keys(Settings(approval_signing_key=SecretStr("")))
