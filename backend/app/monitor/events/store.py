"""Persistence for monitor events: idempotent insert and derived per-entity progress."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.monitor import MonitorEventRecord
from app.monitor.events.completion import EntityProgress
from app.schemas.monitor import EventOutcome


def _order_key(row: MonitorEventRecord) -> tuple[object, ...]:
    # SQLite returns naive datetimes (stored as UTC); compare on the naive value so both flavours agree.
    return (row.occurred_at.replace(tzinfo=None), row.rank, row.received_at.replace(tzinfo=None))


async def applied_rows(session: AsyncSession, run_id: str, entity_id: str | None = None) -> list[MonitorEventRecord]:
    stmt = select(MonitorEventRecord).where(
        MonitorEventRecord.run_id == run_id, MonitorEventRecord.status == EventOutcome.APPLIED.value
    )
    if entity_id is not None:
        stmt = stmt.where(MonitorEventRecord.entity_id == entity_id)
    return list((await session.execute(stmt)).scalars())


def latest_by_entity(rows: list[MonitorEventRecord]) -> dict[str, MonitorEventRecord]:
    """Newest applied event per entity by ``(occurred_at, rank)``, independent of arrival order."""
    latest: dict[str, MonitorEventRecord] = {}
    for row in rows:
        current = latest.get(row.entity_id)
        if current is None or _order_key(row) > _order_key(current):
            latest[row.entity_id] = row
    return latest


def progress(rows: list[MonitorEventRecord]) -> dict[str, EntityProgress]:
    return {k: EntityProgress(r.event_type, r.problem) for k, r in latest_by_entity(rows).items()}


async def exists(session: AsyncSession, source: str, event_id: str) -> bool:
    stmt = select(MonitorEventRecord.id).where(
        MonitorEventRecord.source == source, MonitorEventRecord.event_id == event_id
    )
    return (await session.execute(stmt)).first() is not None


async def insert(session: AsyncSession, row: MonitorEventRecord) -> bool:
    """Insert ``row``; ``False`` if another writer inserted the same ``(source, event_id)`` first."""
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        return False
    return True


def is_stale(row: MonitorEventRecord, latest: MonitorEventRecord | None) -> bool:
    return latest is not None and _order_key(row) < _order_key(latest)
