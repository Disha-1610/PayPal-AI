"""Periodic payment reconciliation loop. A failed pass is logged and retried on the next tick; it never stops the loop."""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.payments.reconciliation import Reconciler

logger = get_logger(__name__)


async def run_reconciler(
    reconciler: Reconciler, factory: async_sessionmaker[AsyncSession], *, interval_seconds: float, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            summary = await reconciler.sweep(factory)
            logger.info("payment reconciliation finished", extra=summary.__dict__)
        except Exception:
            logger.exception("payment reconciliation failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
