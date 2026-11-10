"""The milestone plan a user approves for a contractor engagement.

The whole plan is hashed (``plan_hash``): amounts, due dates, criteria and contractor are fixed once
approved. Any change - by the contractor, a counter-offer or the agent - produces a different hash and
therefore needs a fresh user approval; there is deliberately no amend operation.
"""

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.hashing import stable_hash


class MilestoneSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    description: str = Field(min_length=1, max_length=512, description="Expected deliverable")
    amount: Decimal = Field(gt=0, decimal_places=2)
    due_date: datetime
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)

    @field_validator("due_date")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("due_date must be timezone-aware")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _non_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("acceptance criteria must not be blank")
        return value

    def criteria_hash(self) -> str:
        return stable_hash(list(self.acceptance_criteria))


class MilestonePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str = Field(min_length=1, max_length=64)
    contractor_id: str = Field(min_length=1, max_length=128)
    currency: str = Field(min_length=3, max_length=3)
    budget: Decimal = Field(gt=0, description="The user's hard ceiling for the whole project")
    milestones: tuple[MilestoneSpec, ...] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check(self) -> Self:
        total = sum((m.amount for m in self.milestones), Decimal(0))
        if total > self.budget:
            raise ValueError(f"milestones total {total} exceeds the budget {self.budget}")
        dates = [m.due_date for m in self.milestones]
        if dates != sorted(dates):
            raise ValueError("milestone due dates must not go backwards")
        return self

    def canonical(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "contractor_id": self.contractor_id,
            "currency": self.currency,
            "budget": str(self.budget),
            "milestones": [
                {
                    "description": m.description,
                    "amount": str(m.amount),
                    "due_date": m.due_date.isoformat(),
                    "acceptance_criteria": list(m.acceptance_criteria),
                }
                for m in self.milestones
            ],
        }

    def plan_hash(self) -> str:
        return stable_hash(self.canonical())
