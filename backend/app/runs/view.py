"""Client-facing run shape (mirrors the frontend ``Run`` type). Built only from trusted fields: this is the
boundary where checkpointed state is projected for the browser, so nothing untrusted flows through it.

Index-only projection for now (identity, status, budget); candidates, steps, messages and payment are added
from the checkpointed ``TaskState`` by the state mapper."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.db.models.run import RunRecord

RunStatus = Literal[
    "running", "awaiting_input", "awaiting_approval", "awaiting_payment", "completed", "failed", "stopped"
]
_TERMINAL = {"completed": "completed", "failed": "failed", "stopped": "stopped", "cancelled": "stopped"}
_WAITING = {
    "awaiting_approval": "awaiting_approval",
    "awaiting_payer": "awaiting_payment",
    "awaiting_input": "awaiting_input",
}


class PlanStepView(BaseModel):
    id: str
    title: str
    agent: str
    status: str


class RunView(BaseModel):
    id: str
    goal: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    budget_usd: float
    iteration: int = 0
    max_iterations: int = 0
    steps: list[PlanStepView] = []


class RunPageView(BaseModel):
    items: list[RunView]
    next_cursor: str | None = None


def client_status(status: str) -> RunStatus:
    """Map an internal graph status onto the small set the UI understands; unknown means still working."""
    return _TERMINAL.get(status) or _WAITING.get(status) or "running"  # type: ignore[return-value]


def run_view(record: RunRecord) -> RunView:
    return RunView(
        id=record.run_id, goal=record.goal, status=client_status(record.status), created_at=record.created_at,
        updated_at=record.updated_at, budget_usd=float(record.max_spend),
    )
