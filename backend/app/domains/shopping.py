from app.domains.base import DomainPlugin, FieldSpec, ValueType
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
)
