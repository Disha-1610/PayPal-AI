"""Per-invocation LLM time budget.

``RunController`` opens a budget around every graph invocation. Once it is spent, ``gateway.call``
refuses new LLM calls with ``LLMGatewayError``, which every agent already handles by falling back to
its deterministic path (code-only scoring, rule-based coordination, no query rewrite). In-flight
attempts are capped to the time that remains.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("llm_deadline", default=None)


@contextmanager
def llm_budget(seconds: float | None) -> Iterator[None]:
    """Allow LLM calls for ``seconds`` from now; ``None`` or <= 0 means unlimited."""
    token = _deadline.set(time.monotonic() + seconds if seconds and seconds > 0 else None)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining() -> float | None:
    """Seconds left in the budget (may be <= 0), or ``None`` when no budget is active."""
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()
