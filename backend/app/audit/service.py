"""Audit record writing.

* Critical (security / payment) actions: ``record_critical`` inserts the audit row inside the
  SAME database transaction as the action it guards. If the audit insert fails, the exception
  propagates and the transaction (and therefore the action) is rolled back, so the action can
  never be committed without a durable audit record (transactional-outbox guarantee).
* Non-critical events: ``AuditWriter`` queues events and persists them in a background task
  so the graph is never blocked. Failures are logged, never silently dropped.
"""

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.hashing import stable_hash
from app.core.logging import get_logger
from app.db.models.audit import AuditLog
from app.db.session import SessionFactory

logger = get_logger(__name__)


class AuditEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    phase: str | None = None
    node: str
    actor_type: str
    actor_id: str | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    input_hash: str | None = None
    decision: str | None = None
    reason: str | None = None
    state_diff: dict[str, Any] | None = None
    metadata_json: dict[str, Any] | None = None


def hash_input(payload: object) -> str:
    """Stable sha256 of a JSON-serialisable node input (raw input is not stored)."""
    return stable_hash(payload)


def _to_row(event: AuditEvent) -> AuditLog:
    return AuditLog(**event.model_dump())


async def record_critical(session: AsyncSession, event: AuditEvent) -> None:
    """Insert the audit row in the caller's transaction; raises if it cannot be written."""
    session.add(_to_row(event))
    await session.flush()


class AuditWriter:
    """Background writer for non-critical audit events."""

    def __init__(self, factory: async_sessionmaker[AsyncSession] = SessionFactory, maxsize: int = 1000) -> None:
        self._factory = factory
        self._queue: asyncio.Queue[AuditEvent] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="audit-writer")

    async def stop(self) -> None:
        """Drain pending events, then stop."""
        if self._task is None:
            return
        await self._queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def enqueue(self, event: AuditEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.error("audit queue full; event dropped", extra={"run_id": event.run_id, "action": event.action})

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                async with self._factory() as session:
                    async with session.begin():
                        await record_critical(session, event)
            except Exception:
                logger.exception("failed to persist audit event", extra={"run_id": event.run_id, "action": event.action})
            finally:
                self._queue.task_done()
