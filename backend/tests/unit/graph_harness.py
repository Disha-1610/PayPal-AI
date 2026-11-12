"""The real compiled shopping graph with mock connectors and a scripted LLM, for end-to-end route tests."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from langgraph.checkpoint.memory import MemorySaver

from app.connectors.mocks.amazon import build_mock_amazon
from app.domains.shopping import SHOPPING
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.runtime import RunController
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.state import TaskState, make_initial_state
from tests.unit.test_connectors import MemoryTextStore, make_registry

INTAKE = "Running shoes. Budget: 5000 INR."


class Sink:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def enqueue(self, event) -> None:
        self.actions.append(event.action)


def extraction(goal: str = "running shoes", *, budget: int | None = 5000, conflicts: list[str] | None = None) -> str:
    constraints = (
        [{"field": "budget", "operator": "lte", "value": budget, "strength": "hard", "evidence": f"Budget: {budget} INR"}]
        if budget is not None
        else []
    )
    return json.dumps({"goal": goal, "constraints": constraints, "conflicts": conflicts or []})


def deps(provider, **overrides) -> GraphDeps:
    base = dict(
        plugin=SHOPPING, registry=make_registry(MemoryTextStore(), build_mock_amazon()), text_source=MagicMock(),
        executor=MagicMock(), session_factory=MagicMock(), audit_sink=Sink(), terms_for=MagicMock(),
        approval_keys={"v1": b"k" * 32}, provider=provider,
    )
    return GraphDeps(**{**base, **overrides})


def controller(provider, **overrides) -> RunController:
    return RunController(build_graph(deps(provider, **overrides), MemorySaver()))


def initial(run_id: str = "r1", text: str = INTAKE) -> TaskState:
    auth = TaskAuthorization(
        run_id=run_id, max_spend=Decimal("5000"), currency="INR", permitted_platforms=frozenset({"mock_amazon"}),
        permitted_actions=frozenset({Action.SEARCH, Action.CREATE_ORDER}), expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    return make_initial_state(run_id=run_id, original_intent=text, authorization=auth, domain="shopping")
