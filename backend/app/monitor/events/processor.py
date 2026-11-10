"""Shared Monitor engine: validate, deduplicate, order, interpret, check completion.

One engine serves every domain; what an event *means* comes from the domain's ``MonitorSpec``. The
processor detects and records; it never changes constraints or moves money. The router decides what
runs next. It writes in the caller's transaction (the caller commits), and audits every decision in
that same transaction.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import Phase
from app.audit.service import AuditEvent, hash_input, record_critical
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.models.monitor import MonitorEventRecord
from app.domains import get_plugin
from app.monitor import ledger
from app.monitor.events import store
from app.monitor.events.completion import check_completion
from app.monitor.events.deadline import Assessment, InvalidEvent, assess_event
from app.schemas.monitor import EventOutcome, MonitorEvent, MonitorResult, TaskView

logger = get_logger(__name__)

# Loads the persisted task view for a run; raises ``LookupError`` for an unknown run.
TaskLoader = Callable[[str], Awaitable[TaskView]]

MAX_FUTURE_SKEW = timedelta(minutes=5)


class EventProcessor:
    def __init__(
        self,
        load_task: TaskLoader,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_future_skew: timedelta = MAX_FUTURE_SKEW,
    ) -> None:
        self._load_task = load_task
        self._clock = clock
        self._max_future_skew = max_future_skew

    async def process(self, session: AsyncSession, event: MonitorEvent) -> MonitorResult:
        if await store.exists(session, event.source, event.event_id):
            logger.info("duplicate monitor event", extra={"run_id": event.run_id, "event_id": event.event_id})
            return MonitorResult(event=event, outcome=EventOutcome.DUPLICATE, reason="already processed")

        try:
            view = await self._load_task(event.run_id)
        except LookupError:
            return await self._reject(session, event, "unknown task", rank=0)
        if view.domain != event.domain:
            return await self._reject(session, event, "event domain does not match the task", rank=0)
        rule = get_plugin(view.domain).monitor.rule(event.event_type)
        if rule is None:
            return await self._reject(session, event, "unknown event type for this domain", rank=0)
        if event.occurred_at > self._clock() + self._max_future_skew:
            return await self._reject(session, event, "event is dated in the future", rank=rule.rank)
        try:
            assessment = assess_event(rule, event, view)
        except InvalidEvent as exc:
            return await self._reject(session, event, str(exc), rank=rule.rank)

        row = self._row(event, rule.rank, EventOutcome.APPLIED, assessment, None)
        latest = store.latest_by_entity(await store.applied_rows(session, event.run_id, event.entity_id)).get(
            event.entity_id
        )
        if store.is_stale(row, latest):
            row.status, row.problem, row.reason = EventOutcome.STALE.value, None, "older than the latest event"
        if not await store.insert(session, row):  # a concurrent delivery won the race
            return MonitorResult(event=event, outcome=EventOutcome.DUPLICATE, reason="already processed")
        await self._audit(session, event, row)

        payment_statuses = await ledger.statuses(session, event.run_id)
        if row.status == EventOutcome.STALE.value:
            return MonitorResult(
                event=event, outcome=EventOutcome.STALE, reason=row.reason,
                payment_committed=ledger.is_committed(payment_statuses),
            )
        completion = check_completion(
            get_plugin(view.domain).monitor,
            store.progress(await store.applied_rows(session, event.run_id)),
            ledger.summarize(payment_statuses),
            view.expected_entities,
        )
        return MonitorResult(
            event=event,
            outcome=EventOutcome.APPLIED,
            problem=assessment.problem,
            affected_constraint_id=assessment.constraint_id,
            deadline=assessment.deadline,
            new_date=assessment.new_date,
            payment_committed=ledger.is_committed(payment_statuses),
            completed=completion.done,
            blockers=completion.blockers,
        )

    async def _reject(self, session: AsyncSession, event: MonitorEvent, reason: str, *, rank: int) -> MonitorResult:
        row = self._row(event, rank, EventOutcome.REJECTED, Assessment(), reason)
        if not await store.insert(session, row):
            return MonitorResult(event=event, outcome=EventOutcome.DUPLICATE, reason="already processed")
        await self._audit(session, event, row)
        return MonitorResult(event=event, outcome=EventOutcome.REJECTED, reason=reason)

    def _row(
        self, event: MonitorEvent, rank: int, outcome: EventOutcome, assessment: Assessment, reason: str | None
    ) -> MonitorEventRecord:
        return MonitorEventRecord(
            event_id=event.event_id, source=event.source, run_id=event.run_id, domain=event.domain,
            event_type=event.event_type, entity_id=event.entity_id, occurred_at=event.occurred_at,
            received_at=self._clock(), rank=rank, status=outcome.value,
            problem=assessment.problem.value if assessment.problem else None, reason=reason, payload=event.payload,
        )

    async def _audit(self, session: AsyncSession, event: MonitorEvent, row: MonitorEventRecord) -> None:
        await record_critical(
            session,
            AuditEvent(
                run_id=event.run_id, phase=Phase.MONITOR, node="monitor", actor_type="system", actor_id=event.source,
                action="MONITOR_EVENT", resource_type=event.domain, resource_id=event.entity_id,
                input_hash=hash_input(event.model_dump(mode="json")), decision=row.status, reason=row.reason,
                metadata_json={"event_type": event.event_type, "problem": row.problem},
            ),
        )
