"""Periodic deadline sweep. Tasks live for days, so this loop (not a request) notices a passed deadline.

Event ids are derived from (run, entity, deadline), so a sweep that overlaps another, or repeats after a crash,
is a harmless duplicate. A failed sweep is logged and retried on the next tick; it never stops the loop.
"""

import asyncio

from app.core.logging import get_logger
from app.monitor.events.processor import EventProcessor
from app.workers.monitor import MonitorWorker

logger = get_logger(__name__)


async def run_sweeper(
    worker: MonitorWorker, processor: EventProcessor, *, interval_seconds: float, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            handled = await worker.sweep(processor)
            logger.info("deadline sweep finished", extra={"handled": handled})
        except Exception:
            logger.exception("deadline sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
