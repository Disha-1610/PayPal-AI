from app.domains.base import DomainPlugin, EventRule, FieldSpec, MonitorSpec, ValueType
from app.schemas.monitor import Problem
from app.schemas.plan import PlanStep

SHOPPING = DomainPlugin(
    name="shopping",
    description="Find and buy a product from online stores.",
    goal_question="What would you like to buy?",
    fields=(
        FieldSpec(name="budget", value_type=ValueType.NUMBER, description="Maximum price the user will pay"),
        FieldSpec(name="brand", value_type=ValueType.TEXT, description="Brand name"),
        FieldSpec(name="color", value_type=ValueType.TEXT, description="Colour"),
        FieldSpec(name="size", value_type=ValueType.TEXT, description="Size"),
        FieldSpec(name="delivery_days", value_type=ValueType.NUMBER, description="Maximum days until delivery"),
    ),
    plan_steps=(
        PlanStep(step_id="intent", description="Extract goal and constraints"),
        PlanStep(step_id="discover", description="Search stores for candidates", depends_on=("intent",)),
        PlanStep(step_id="evaluate", description="Rank candidates against constraints", depends_on=("discover",)),
        PlanStep(step_id="negotiate", description="Negotiate price with the top candidate", depends_on=("evaluate",)),
        PlanStep(step_id="verify", description="Verify the offer before approval", depends_on=("negotiate",)),
        PlanStep(step_id="approval", description="Ask the user to approve the purchase", depends_on=("verify",)),
        PlanStep(step_id="order", description="Create the order and capture payment", depends_on=("approval",)),
    ),
    monitor=MonitorSpec(
        rules=(
            EventRule(event_type="ORDER_PLACED", rank=10),
            EventRule(event_type="SHIPPED", rank=20),
            EventRule(
                event_type="DELIVERY_DELAYED", rank=20, problem=Problem.DEADLINE_RISK,
                deadline_field="delivery_days", date_key="new_date",
            ),
            EventRule(event_type="DEADLINE_PASSED", rank=20, problem=Problem.DEADLINE_RISK),
            EventRule(event_type="DELIVERY_FAILED", rank=30, problem=Problem.NON_DELIVERY),
            EventRule(event_type="DELIVERED", rank=40),
            EventRule(event_type="RETURNED", rank=50, problem=Problem.NON_DELIVERY),
        ),
        completed_event_types=frozenset({"DELIVERED"}),
        deadline_field="delivery_days",
    ),
)
