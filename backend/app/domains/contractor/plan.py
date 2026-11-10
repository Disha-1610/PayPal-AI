from app.domains.base import DomainPlugin, EventRule, FieldSpec, MonitorSpec, ValueType
from app.schemas.monitor import Problem
from app.schemas.plan import PlanStep

CONTRACTOR = DomainPlugin(
    name="contractor",
    description="Hire a contractor or freelancer for a fixed piece of work.",
    goal_question="What work do you want the contractor to do?",
    fields=(
        FieldSpec(
            name="budget",
            value_type=ValueType.NUMBER,
            description="Maximum total payment",
            blocking=True,
            question="What is the maximum budget for this work?",
        ),
        FieldSpec(
            name="compliance_terms",
            value_type=ValueType.TEXT,
            description="Legal or compliance requirements (contract type, licences, NDA, tax)",
            blocking=True,
            question="Are there legal or compliance terms the contractor must meet (contract type, NDA, licences)?",
        ),
        FieldSpec(name="duration", value_type=ValueType.TEXT, description="Length of the engagement"),
        FieldSpec(name="skills", value_type=ValueType.LIST, description="Required skills"),
        FieldSpec(name="location", value_type=ValueType.TEXT, description="Where the work is done"),
        FieldSpec(name="start_date", value_type=ValueType.TEXT, description="When work should begin"),
    ),
    plan_steps=(
        PlanStep(step_id="intent", description="Extract goal and constraints"),
        PlanStep(step_id="discover", description="Find matching contractors", depends_on=("intent",)),
        PlanStep(step_id="evaluate", description="Rank contractors against constraints", depends_on=("discover",)),
        PlanStep(step_id="negotiate", description="Negotiate rate and terms", depends_on=("evaluate",)),
        PlanStep(step_id="verify", description="Verify credentials and compliance", depends_on=("negotiate",)),
        PlanStep(step_id="approval", description="Ask the user to approve the engagement", depends_on=("verify",)),
        PlanStep(
            step_id="engage",
            description="Create milestone records; authorize, verify and capture each milestone in turn",
            depends_on=("approval",),
        ),
    ),
    # No deadline field exists for this domain, so only an explicit overdue report raises deadline risk.
    monitor=MonitorSpec(
        rules=(
            EventRule(event_type="MILESTONE_STARTED", rank=10),
            EventRule(event_type="MILESTONE_SUBMITTED", rank=20),
            EventRule(event_type="MILESTONE_OVERDUE", rank=20, problem=Problem.DEADLINE_RISK),
            EventRule(event_type="MILESTONE_REJECTED", rank=30, problem=Problem.WORK_REJECTED),
            EventRule(event_type="MILESTONE_VERIFIED", rank=40),
        ),
        completed_event_types=frozenset({"MILESTONE_VERIFIED"}),
    ),
)
