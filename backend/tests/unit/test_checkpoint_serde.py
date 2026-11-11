"""Checkpoints round-trip our schema types through the allow-listed serializer, and nothing else is rebuilt."""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.serde import jsonplus
from pydantic import BaseModel

from app.core.config import Settings
from app.domains.shopping import SHOPPING
from app.orchestrator.graph.builder import GraphDeps, build_graph
from app.orchestrator.graph.checkpointer import checkpoint_serde, open_checkpointer, schema_types
from app.orchestrator.graph.runtime import RunController
from app.schemas.adapt import AdaptProgress
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, Operator

AUTH = TaskAuthorization(
    run_id="r1", max_spend=Decimal(100), currency="USD", permitted_platforms=frozenset({"channel3"}),
    permitted_actions=frozenset({Action.SEARCH}),
)
CAND = Candidate(candidate_id="c1", source="channel3", domain="shopping", title="Shoe", total_cost=Decimal("9.99"), currency="USD")


def test_schema_types_cover_the_state_models():
    types = set(schema_types())
    assert {("app.schemas.authorization", "TaskAuthorization"), ("app.schemas.candidate", "Candidate"),
            ("app.schemas.adapt", "AdaptProgress"), ("app.schemas.authorization", "Action")} <= types


def test_only_allow_listed_types_are_rebuilt():
    serde = checkpoint_serde()
    assert serde.loads_typed(serde.dumps_typed(AUTH)) == AUTH

    class Foreign(BaseModel):
        x: int = 1

    foreign = serde.dumps_typed(Foreign())
    rebuilt = serde.loads_typed(foreign)
    assert not isinstance(rebuilt, Foreign)  # an unlisted class is not instantiated from stored bytes


async def test_real_sqlite_checkpointer_round_trips_task_state_across_a_restart(tmp_path, caplog):
    cfg = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'a.sqlite'}", checkpoint_sqlite_path=str(tmp_path / "cp.sqlite"))
    deps = GraphDeps(plugin=SHOPPING, registry=MagicMock(), text_source=MagicMock(), executor=MagicMock(), session_factory=MagicMock(),
                     audit_sink=MagicMock(), terms_for=MagicMock(), approval_keys={"v1": b"k" * 32})
    constraint = Constraint(field_name="budget", operator=Operator.LTE, target_value=100, source=ConstraintSource.USER)
    values = {"run_id": "r1", "goal": "shoes", "domain": "shopping", "status": "searching", "authorization": AUTH,
              "candidates": [CAND], "constraints": [constraint], "adapt": AdaptProgress(),
              "anchor_at": datetime(2026, 11, 11, tzinfo=UTC)}
    jsonplus._warned_unregistered_types.clear()  # the library warns once per process; make the assertion meaningful
    with caplog.at_level(logging.WARNING):
        async with open_checkpointer(cfg) as saver:
            graph = build_graph(deps, saver)
            await graph.aupdate_state({"configurable": {"thread_id": "r1"}}, values, as_node="settle")
        async with open_checkpointer(cfg) as saver:  # a fresh connection, as after a worker restart
            state = await RunController(build_graph(deps, saver)).state("r1")
    assert state["authorization"] == AUTH and state["candidates"] == [CAND] and state["constraints"] == [constraint]
    assert isinstance(state["adapt"], AdaptProgress) and state["anchor_at"] == values["anchor_at"]
    assert "unregistered" not in caplog.text.lower()


async def test_monitor_replan_re_enters_a_finished_run_at_adapt_on_the_real_graph():
    from langgraph.checkpoint.memory import MemorySaver

    deps = GraphDeps(plugin=SHOPPING, registry=MagicMock(), text_source=MagicMock(), executor=MagicMock(), session_factory=MagicMock(),
                     audit_sink=MagicMock(), terms_for=MagicMock(), approval_keys={"v1": b"k" * 32})
    graph = build_graph(deps, MemorySaver())
    config = {"configurable": {"thread_id": "r1"}}
    await graph.aupdate_state(config, {"run_id": "r1", "goal": "shoes", "domain": "shopping", "status": "awaiting_confirmation",
                                       "authorization": AUTH, "original_intent": "shoes"}, as_node="settle")
    assert (await graph.aget_state(config)).next == ()  # the run had ended
    await graph.aupdate_state(config, {}, as_node="monitor_intake")
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("adapt",) and snapshot.values["status"] == "awaiting_confirmation"
    out = await RunController(graph).replan("r1")  # and it actually runs: Adapt decides, the run does not hang
    assert out["status"] in {"stopped", "failed", "awaiting_input", "replanning", "adapting", "searching"}
