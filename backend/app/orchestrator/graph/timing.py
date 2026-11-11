"""Per-node wall-clock timing: one ``node_completed`` log line per node execution."""

import functools
import inspect
import time
from collections.abc import Callable
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)


def timed(name: str, node: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a graph node so its duration and outcome are logged. Sync/async nature is preserved."""

    def log(state: Any, started: float, outcome: str) -> None:
        run_id = state.get("run_id") if hasattr(state, "get") else None
        logger.info(
            "node_completed",
            extra={"node": name, "run_id": run_id, "duration_ms": round((time.perf_counter() - started) * 1000), "outcome": outcome},
        )

    if inspect.iscoroutinefunction(node):

        @functools.wraps(node)
        async def run_async(state: Any) -> Any:
            started = time.perf_counter()
            try:
                result = await node(state)
            except BaseException as exc:  # includes LangGraph interrupts; always re-raised
                log(state, started, type(exc).__name__)
                raise
            log(state, started, "ok")
            return result

        return run_async

    @functools.wraps(node)
    def run_sync(state: Any) -> Any:
        started = time.perf_counter()
        try:
            result = node(state)
        except BaseException as exc:
            log(state, started, type(exc).__name__)
            raise
        log(state, started, "ok")
        return result

    return run_sync
