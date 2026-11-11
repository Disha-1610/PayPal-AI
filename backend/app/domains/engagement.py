"""Execution adapters for the negotiating domains (contractor, A2A): the exact terms a user approves and the
bounds a renegotiation must stay inside.

An engagement is one fixed-price piece of work with a hard completion deadline (the user's ``deliver_by``),
paid through PayPal to the platform's payee and captured only after the work is verified.

The counterparty is a separate party (``counterparty_id``) from the payee (``payee_id``). A counter-offer may
change who the counterparty is (that needs a fresh approval, like any change), but it can never redirect the
payment: the negotiation envelope's seller is the counterparty, so ``payee_id`` is not negotiable.
"""

from decimal import Decimal

from app.domains.a2a import negotiation_envelope
from app.orchestrator.graph.negotiation import EnvelopeFor
from app.schemas.approval_terms import ApprovalTerms, to_minor_units
from app.schemas.candidate import Candidate
from app.schemas.negotiator import NegotiationEnvelope
from app.schemas.state import TaskState

COUNTERPARTY_PARTY = "counterparty_id"


class EngagementTerms:
    """``GraphDeps.terms_for`` for contractor and A2A runs."""

    def __init__(self, payee_id: str, *, permission: str) -> None:
        if not payee_id:
            raise ValueError("a payee merchant id is required")
        if not permission:
            raise ValueError("a permission name is required")
        self._payee_id, self._permission = payee_id, permission

    def __call__(self, candidate: Candidate, state: TaskState) -> ApprovalTerms:
        if candidate.total_cost is None or candidate.currency is None:
            raise ValueError(f"candidate {candidate.candidate_id} has no known total cost; it cannot be approved")
        deadline = state.get("deliver_by")
        if deadline is None:
            raise ValueError("an engagement needs the user's completion deadline (deliver_by)")
        minor = to_minor_units(candidate.total_cost, candidate.currency)
        return ApprovalTerms(
            subject_id=candidate.candidate_id,
            subject_attributes={"title": candidate.title},
            parties={"payee_id": self._payee_id, COUNTERPARTY_PARTY: candidate.source},
            currency=candidate.currency,
            max_total_minor=minor,
            expected_total_minor=minor,
            latest_acceptable_at=deadline,
            permissions=frozenset({self._permission}),
            domain_data={"platform": candidate.source, "run_id": state["run_id"]},
            display={"title": candidate.title, "seller": candidate.source},
        )


class PlanTerms:
    """``GraphDeps.terms_for`` for milestone engagements: the engagement terms for the whole proposed plan, bound to
    its hash. Approving these lets the plan be stored; each milestone is then approved and paid on its own."""

    def __init__(self, base: EngagementTerms) -> None:
        self._base = base

    def __call__(self, candidate: Candidate, state: TaskState) -> ApprovalTerms:
        plan = state.get("milestone_plan")
        if plan is None:
            raise ValueError("no milestone plan has been proposed for this candidate")
        terms = self._base(candidate, state)
        if plan.currency != terms.currency:
            raise ValueError("the milestone plan and the contractor's price use different currencies")
        total = to_minor_units(sum((m.amount for m in plan.milestones), Decimal(0)), plan.currency)
        return terms.model_copy(
            update={
                "max_total_minor": total, "expected_total_minor": total,
                "domain_data": {**terms.domain_data, "plan_hash": plan.plan_hash(), "milestones": len(plan.milestones)},
                "display": {"title": f"{candidate.title} ({len(plan.milestones)} milestones)", "seller": candidate.source},
            }
        )


def engagement_envelope(ceiling_field: str) -> EnvelopeFor:
    """Negotiation bounds from the approved terms, capped by the user's ``ceiling_field`` constraint."""

    def build(terms: ApprovalTerms, state: TaskState) -> NegotiationEnvelope:
        return negotiation_envelope(
            terms, list(state.get("constraints", [])), ceiling_field=ceiling_field, seller_party=COUNTERPARTY_PARTY
        )

    return build


__all__ = ["COUNTERPARTY_PARTY", "EngagementTerms", "PlanTerms", "engagement_envelope"]
