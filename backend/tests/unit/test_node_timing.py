import logging

import pytest

from app.orchestrator.graph.timing import timed


async def test_async_node_logs_duration_and_returns_result(caplog):
    async def node(state):
        return {"status": "ok"}

    with caplog.at_level(logging.INFO, logger="app.orchestrator.graph.timing"):
        assert await timed("demo", node)({"run_id": "r1"}) == {"status": "ok"}
    rec = next(r for r in caplog.records if r.message == "node_completed")
    assert (rec.node, rec.run_id, rec.outcome) == ("demo", "r1", "ok")
    assert rec.duration_ms >= 0


def test_sync_node_reraises_and_records_outcome(caplog):
    def node(state):
        raise ValueError("boom")

    with caplog.at_level(logging.INFO, logger="app.orchestrator.graph.timing"):
        with pytest.raises(ValueError):
            timed("demo", node)({"run_id": "r2"})
    assert next(r for r in caplog.records if r.message == "node_completed").outcome == "ValueError"
