"""Timer-driven checks: raise ``DEADLINE_PASSED`` when a deadline has gone by with work unfinished.

Tasks live for days, so this runs from a periodic worker over active runs; no request stays open.
Event ids are derived from ``(run, entity, deadline)``, so a repeated sweep is a harmless duplicate.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.domains import get_plugin
from app.monitor.events import store
from app.monitor.events.completion import PAYMENT_ENTITY_PREFIX
from app.monitor.events.deadline import resolve_deadline
from app.schemas.monitor import MonitorEvent, TaskView

SOURCE = "timer"
EVENT_TYPE = "DEADLINE_PASSED"


async def overdue_events(session: AsyncSession, view: TaskView, *, now: datetime | None = None) -> list[MonitorEvent]:
    """One event per unfinished entity when the domain's deadline constraint has passed; else none."""
    now = now or utcnow()
    spec = get_plugin(view.domain).monitor
    if spec.deadline_field is None or spec.rule(EVENT_TYPE) is None:
        return []
    deadline = resolve_deadline(view, spec.deadline_field)
    if deadline is None or now <= deadline.at:
        return []
    latest = store.progress(await store.applied_rows(session, view.run_id))
    entities = view.expected_entities or frozenset(k for k in latest if not k.startswith(PAYMENT_ENTITY_PREFIX))
    return [
        MonitorEvent(
            event_id=f"deadline-passed:{view.run_id}:{entity}:{deadline.at.isoformat()}", run_id=view.run_id,
            source=SOURCE, domain=view.domain, event_type=EVENT_TYPE, entity_id=entity, occurred_at=now,
            payload={"deadline": deadline.at.isoformat()},
        )
        for entity in sorted(entities)
        if (progress := latest.get(entity)) is None or progress.event_type not in spec.completed_event_types
    ]
