from decimal import Decimal

from app.domains.base import DomainPlugin, EventRule, FieldSpec, MonitorSpec, ValueType
from app.orchestrator.agents.negotiator.offer import envelope_from_terms
from app.schemas.approval_terms import ApprovalTerms, to_minor_units
from app.schemas.constraints import Constraint
from app.schemas.monitor import Problem
from app.schemas.negotiator import NegotiationEnvelope
from app.schemas.plan import PlanStep

# Credentials and spend authority are granted through ``TaskAuthorization``, never extracted from chat.
A2A = DomainPlugin(
    name="a2a",
    description="Negotiate a deal with another agent on the user's behalf.",
    goal_question="What do you want to negotiate with the other agent?",
    fields=(
        FieldSpec(
            name="price_ceiling",
            value_type=ValueType.NUMBER,
            description="Highest price the agent may agree to",
            blocking=True,
            question="What is the highest price I may agree to?",
        ),
        FieldSpec(
            name="counterparty",
            value_type=ValueType.TEXT,
            description="The agent or service to negotiate with",
            blocking=True,
            question="Which agent or service should I negotiate with?",
        ),
        FieldSpec(name="deadline", value_type=ValueType.TEXT, description="When the deal must be concluded"),
        FieldSpec(name="quantity", value_type=ValueType.NUMBER, description="Quantity or volume"),
    ),
    plan_steps=(
        PlanStep(step_id="intent", description="Extract goal and constraints"),
        PlanStep(step_id="handshake", description="Discover counterparty capabilities", depends_on=("intent",)),
        PlanStep(step_id="negotiate", description="Negotiate within the price ceiling", depends_on=("handshake",)),
        PlanStep(step_id="verify", description="Verify the agreed terms", depends_on=("negotiate",)),
        PlanStep(step_id="approval", description="Ask the user to approve the deal", depends_on=("verify",)),
        PlanStep(step_id="settle", description="Settle payment", depends_on=("approval",)),
    ),
    monitor=MonitorSpec(
        rules=(
            EventRule(event_type="TASK_ACCEPTED", rank=10),
            EventRule(event_type="PROGRESS_UPDATED", rank=20),
            EventRule(
                event_type="ETA_UPDATED", rank=20, problem=Problem.DEADLINE_RISK,
                deadline_field="deadline", date_key="new_eta",
            ),
            EventRule(event_type="DEADLINE_PASSED", rank=20, problem=Problem.DEADLINE_RISK),
            EventRule(event_type="TASK_FAILED", rank=30, problem=Problem.WORK_REJECTED),
            EventRule(event_type="RESULT_SUBMITTED", rank=30),
            EventRule(event_type="RESULT_ACCEPTED", rank=40),
        ),
        completed_event_types=frozenset({"RESULT_ACCEPTED"}),
        deadline_field="deadline",
    ),
)


def negotiation_envelope(
    terms: ApprovalTerms, constraints: list[Constraint], *, non_negotiable_terms: dict[str, str] | None = None
) -> NegotiationEnvelope:
    """The user-approved bounds for an A2A deal: the user's ``price_ceiling`` caps any counter-offer.
    Without an active ceiling the approved maximum applies, so the budget is never silently raised."""
    ceilings = [c for c in constraints if c.active and c.field_name == "price_ceiling"]
    ceiling = None
    if ceilings:
        ceiling = to_minor_units(min(Decimal(str(c.target_value)) for c in ceilings), terms.currency)
    return envelope_from_terms(terms, max_total_minor=ceiling, non_negotiable_terms=non_negotiable_terms)
