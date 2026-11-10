from datetime import UTC, datetime, timedelta

import pytest

from app.orchestrator.executor.mismatch import MismatchPolicy, classify_mismatch
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.recheck import ChangeKind, MismatchAction, ObservedTerms

DEADLINE = datetime(2026, 11, 20, tzinfo=UTC)
A = MismatchAction


def approved(**kw) -> ApprovalTerms:
    base = {
        "subject_id": "shoe-1", "subject_attributes": {"size": "M"}, "parties": {"payee_id": "m1"},
        "currency": "INR", "max_total_minor": 550000, "expected_total_minor": 520000,
        "latest_acceptable_at": DEADLINE, "permissions": frozenset({"purchase"}), "constraints": {"returnable": True},
    }
    return ApprovalTerms(**{**base, **kw})


def observed(**kw) -> ObservedTerms:
    base = {
        "available": True, "subject_id": "shoe-1", "subject_attributes": {"size": "M"},
        "parties": {"payee_id": "m1"}, "currency": "INR", "total_minor": 520000,
        "available_by": DEADLINE - timedelta(days=3), "permissions": frozenset({"purchase"}),
        "constraints": {"returnable": True},
    }
    return ObservedTerms(**{**base, **kw})


def kinds(report):
    return {c.kind for c in report.changes}


def test_identical_terms_proceed():
    report = classify_mismatch(approved(), observed())
    assert report.can_proceed and not report.changes


def test_price_drop_within_ceiling_proceeds_with_notice():
    report = classify_mismatch(approved(), observed(total_minor=510000))
    assert report.can_proceed and kinds(report) == {ChangeKind.PRICE_DECREASE}


def test_price_rise_within_ceiling_proceeds_but_above_ceiling_blocks():
    assert classify_mismatch(approved(), observed(total_minor=550000)).can_proceed
    report = classify_mismatch(approved(), observed(total_minor=550001))
    assert report.action is A.REAPPROVE and kinds(report) == {ChangeKind.PRICE_INCREASE}


def test_huge_drop_is_suspicious_not_a_bargain():
    report = classify_mismatch(approved(), observed(total_minor=100000))
    assert report.action is A.REAPPROVE and kinds(report) == {ChangeKind.SUSPICIOUS_DROP}


def test_unavailable_outranks_everything():
    assert classify_mismatch(approved(), observed(available=False, total_minor=999999)).action is A.UNAVAILABLE


def test_deadline_estimate_drift_ok_but_missed_deadline_routes_to_adapt():
    assert classify_mismatch(approved(), observed(available_by=DEADLINE)).can_proceed
    report = classify_mismatch(approved(), observed(available_by=DEADLINE + timedelta(days=2)))
    assert report.action is A.DEADLINE_MISSED


def test_missing_data_is_insufficient_not_assumed_fine():
    assert classify_mismatch(approved(), observed(available_by=None)).action is A.INSUFFICIENT_DATA
    assert classify_mismatch(approved(), observed(total_minor=None)).action is A.INSUFFICIENT_DATA
    assert classify_mismatch(approved(), observed(available=None)).action is A.INSUFFICIENT_DATA


def test_no_deadline_in_terms_ignores_estimate():
    assert classify_mismatch(approved(latest_acceptable_at=None), observed(available_by=None)).can_proceed


@pytest.mark.parametrize(
    "change",
    [
        {"subject_id": "shoe-2"},
        {"subject_attributes": {"size": "L"}},
        {"parties": {"payee_id": "m2"}},
        {"quantity": 2},
        {"currency": "USD"},
        {"constraints": {"returnable": False}},
        {"domain_data": {"gift_wrap": True}},
        {"permissions": frozenset({"purchase", "refund"})},
    ],
)
def test_any_other_change_requires_reapproval(change):
    assert classify_mismatch(approved(), observed(**change)).action is A.REAPPROVE


def test_plugin_can_tolerate_named_fields_only():
    policy = MismatchPolicy(tolerated_fields=frozenset({"domain_data.warehouse"}))
    assert classify_mismatch(approved(), observed(domain_data={"warehouse": "x"}), policy).can_proceed
    assert classify_mismatch(approved(), observed(parties={"payee_id": "m2"}), policy).action is A.REAPPROVE


def test_unreported_keys_are_unknown_not_a_change():
    report = classify_mismatch(approved(), observed(parties={}, subject_attributes={}))
    assert report.action is A.INSUFFICIENT_DATA
    assert {c.kind for c in report.changes} == {ChangeKind.FIELD_UNKNOWN}


def test_extra_descriptive_attributes_are_ignored_but_extra_parties_are_not():
    assert classify_mismatch(approved(), observed(subject_attributes={"size": "M", "brand": "Nike"})).can_proceed
    extra_party = observed(parties={"payee_id": "m1", "intermediary": "x"})
    assert classify_mismatch(approved(), extra_party).action is A.REAPPROVE


def test_naive_observed_datetime_is_rejected_at_the_boundary():
    with pytest.raises(ValueError, match="timezone-aware"):
        observed(available_by=datetime(2026, 11, 18))
