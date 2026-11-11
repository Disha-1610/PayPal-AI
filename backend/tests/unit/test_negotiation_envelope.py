from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domains.a2a import negotiation_envelope as a2a_envelope
from app.domains.contractor.milestone import negotiation_envelope, terms_for
from app.db.models.milestone import Milestone
from app.orchestrator.agents.negotiator.offer import envelope_from_terms
from app.orchestrator.guard.negotiation_guard import check_proposal
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.constraints import Constraint, ConstraintSource, Operator
from app.schemas.negotiator import ProposalDecision, Violation

DUE = datetime(2026, 12, 1, tzinfo=UTC)


def row() -> Milestone:
    return Milestone(
        run_id="r1", project_id="p1", contractor_id="c1", sequence=1, description="d", amount=Decimal("1000"),
        currency="USD", due_date=DUE, acceptance_criteria=["tests pass"], plan_hash="h" * 8, terms_hash="",
    )


def test_unchanged_baseline_is_allowed_and_hash_matches_signed_terms():
    env = negotiation_envelope(row())
    assert env.baseline_terms.terms_hash() == terms_for(row()).terms_hash()
    result = check_proposal(env, env.baseline_offer)
    assert result.decision is ProposalDecision.ALLOWED and result.terms_hash == env.baseline_terms.terms_hash()


def test_default_bounds_never_raise_budget_or_deadline():
    env = negotiation_envelope(row())
    assert env.max_total_minor == 100_000 and env.latest_acceptable_at == DUE
    over = env.baseline_offer.model_copy(update={"price_minor": 100_001})
    late = env.baseline_offer.model_copy(update={"delivery_due": DUE + timedelta(days=1)})
    assert Violation.OVER_BUDGET in check_proposal(env, over).violations
    assert Violation.PAST_DEADLINE in check_proposal(env, late).violations


def test_user_granted_headroom_requires_fresh_approval():
    env = negotiation_envelope(row(), max_total=Decimal("1200"))
    result = check_proposal(env, env.baseline_offer.model_copy(update={"price_minor": 110_000}))
    assert result.decision is ProposalDecision.REQUIRES_APPROVAL


def test_dropping_acceptance_criteria_is_blocked():
    env = negotiation_envelope(row())
    offer = env.baseline_offer.model_copy(update={"terms": {}})
    assert Violation.NON_NEGOTIABLE_TERM in check_proposal(env, offer).violations


def test_bounds_below_approved_terms_are_rejected():
    terms = terms_for(row())
    with pytest.raises(ValueError, match="below the approved price"):
        envelope_from_terms(terms, max_total_minor=99_999)
    with pytest.raises(ValueError, match="before the approved delivery"):
        envelope_from_terms(terms, latest_acceptable_at=DUE - timedelta(days=1))


def test_a2a_ceiling_from_constraints():
    terms = ApprovalTerms(
        subject_id="deal", parties={"payee_id": "agent-2"}, currency="USD", max_total_minor=5_000,
        expected_total_minor=5_000, latest_acceptable_at=DUE,
    )
    ceiling = Constraint(field_name="price_ceiling", operator=Operator.LTE, target_value=60, source=ConstraintSource.USER)
    assert a2a_envelope(terms, [ceiling]).max_total_minor == 6_000
    assert a2a_envelope(terms, []).max_total_minor == 5_000
