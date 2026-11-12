"""Run lifecycle for the API: create (idempotent), list, get. The graph runs in a background task so a request
never waits on LLM/connector calls; the run index row is the source of truth for ownership and idempotency."""

import asyncio
import base64
import binascii
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.milestone import Milestone
from app.db.models.monitor import MonitorEventRecord
from app.db.models.payment import Payment
from app.db.models.run import RunRecord
from app.db.models.run_message import RunMessage
from app.db.session import session_scope
from app.domains import get_plugin
from app.monitor.events.deadline import InvalidEvent, parse_instant
from app.orchestrator.graph.runtime import RunController, RunSnapshot, UnknownRun
from app.runs.view import (
    QUESTION_NODES,
    TERMINAL_STATUSES,
    MessageView,
    RunView,
    can_revise,
    derive_status,
    message_view,
    question_text,
    run_view,
)
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.counterparty import CounterpartyContact
from app.schemas.monitor import Problem
from app.schemas.state import TaskState, make_initial_state

logger = get_logger(__name__)
_EVENT_POLL_SECONDS = 1.5
_HEARTBEAT_SECONDS = 15.0

# Least privilege: no negotiation, refund execution or disputes unless a later feature grants them explicitly.
_GRANTED_ACTIONS = frozenset(
    {
        Action.SEARCH, Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT,
        Action.VOID_PAYMENT, Action.REQUEST_REFUND,
    }
)
_NEGOTIATION_ACTIONS = frozenset({Action.NEGOTIATE})  # granted only to domains whose plug-in negotiates
_AUTHORIZATION_TTL = timedelta(days=7)
_END_OF_DAY = time(23, 59, 59, tzinfo=UTC)


def deadline_of(deliver_by: datetime | None) -> datetime | None:
    """The hard deadline as a timezone-aware instant. A bare date (no zone) means the end of that day in UTC."""
    if deliver_by is None:
        return None
    if deliver_by.tzinfo is None:
        return datetime.combine(deliver_by.date(), _END_OF_DAY)
    return deliver_by


def granted_actions(domain: str) -> frozenset[Action]:
    return _GRANTED_ACTIONS | _NEGOTIATION_ACTIONS if get_plugin(domain).negotiates else _GRANTED_ACTIONS


class InvalidCursor(ValueError):
    """The pagination cursor was not issued by this service."""


class RunBusy(Exception):
    """The graph is executing a step for this run; its state cannot be changed from outside until it pauses."""


class CannotCancel(Exception):
    """The run is already finished, or its committed funds could not be released (so it must stay open)."""


class UnsupportedDomain(Exception):
    """This deployment has no runtime for the requested domain."""


class CounterpartyRequired(Exception):
    """The domain needs a counterparty the user names, or this deployment cannot reach the one given."""


class NotAwaiting(Exception):
    """The run is not paused where this action applies (not asking a question / not waiting for the payer)."""


@dataclass(frozen=True)
class NewRun:
    goal: str
    max_spend: Decimal
    currency: str
    deliver_by: datetime | None = None
    domain: str = "shopping"
    counterparty: CounterpartyContact | None = None


@dataclass(frozen=True)
class RunPage:
    items: list[RunRecord]
    next_cursor: str | None


def intent_text(new: NewRun) -> str:
    """The text the Intent agent extracts constraints from: the goal plus the structured intake fields."""
    parts = [new.goal.strip(), f"Budget: {new.max_spend} {new.currency}."]
    if new.deliver_by is not None:
        parts.append(f"Must be delivered by {new.deliver_by.date().isoformat()}.")
    if new.counterparty is not None:
        parts.append(f"Counterparty: {new.counterparty.describe()}.")
    return " ".join(parts)


def encode_cursor(record: RunRecord) -> str:
    created = record.created_at if record.created_at.tzinfo else record.created_at.replace(tzinfo=UTC)  # SQLite drops tzinfo
    raw = f"{created.astimezone(UTC).isoformat()}|{record.run_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        created, run_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(created), run_id
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor(cursor) from exc


class RunService:
    def __init__(
        self,
        runs: RunController,
        session_factory: async_sessionmaker[AsyncSession],
        platforms: frozenset[str],
        release_funds: Callable[[str], Awaitable[bool]] | None = None,
        *,
        domains: frozenset[str] = frozenset({"shopping"}),
        outreach_domains: frozenset[str] = frozenset(),
        counterparty_required: frozenset[str] = frozenset(),
    ) -> None:
        self._runs, self._factory, self._platforms = runs, session_factory, platforms
        self._domains = domains
        self._outreach = outreach_domains  # domains whose runtime can contact a counterparty the user names
        self._counterparty_required = counterparty_required  # domains with no catalog: a named counterparty is required
        self._release_funds = release_funds  # releases committed money before a cancel; None: nothing to release
        self._tasks: set[asyncio.Task[None]] = set()
        self._resuming: set[str] = set()  # runs with a resume in flight: a second one would re-run the paused node

    async def create(self, user_id: str, idempotency_key: str, new: NewRun) -> RunRecord:
        """Create the run and start it in the background. A repeated key returns the original run."""
        if new.domain not in self._domains:
            raise UnsupportedDomain(f"domain {new.domain!r} is not available; available: {sorted(self._domains)}")
        if new.counterparty is not None and new.domain not in self._outreach:
            raise CounterpartyRequired("contacting a counterparty is not configured on this server")
        if new.counterparty is None and new.domain in self._counterparty_required:
            raise CounterpartyRequired(
                "name who to negotiate with: a contractor's email or phone number, or the other agent's https endpoint"
            )
        existing = await self._by_key(user_id, idempotency_key)
        if existing is not None:
            return existing
        record = RunRecord(
            run_id=uuid4().hex, user_id=user_id, idempotency_key=idempotency_key, goal=new.goal.strip(), domain=new.domain,
            max_spend=new.max_spend, currency=new.currency, deliver_by=deadline_of(new.deliver_by), status="created",
        )
        try:
            async with session_scope(self._factory) as session:
                session.add(record)
        except IntegrityError:  # a concurrent request with the same key won the race
            winner = await self._by_key(user_id, idempotency_key)
            if winner is None:
                raise
            return winner
        self._spawn(record, new)
        return record

    async def get(self, user_id: str, run_id: str) -> RunRecord | None:
        """The run, or None when it does not exist or belongs to someone else (indistinguishable by design)."""
        async with self._factory() as session:
            record = await session.get(RunRecord, run_id)
        return record if record is not None and record.user_id == user_id else None

    async def list(self, user_id: str, *, limit: int, cursor: str | None) -> RunPage:
        query = select(RunRecord).where(RunRecord.user_id == user_id)
        if cursor:
            created, run_id = decode_cursor(cursor)
            query = query.where(
                or_(RunRecord.created_at < created, and_(RunRecord.created_at == created, RunRecord.run_id < run_id))
            )
        query = query.order_by(RunRecord.created_at.desc(), RunRecord.run_id.desc()).limit(limit + 1)
        async with self._factory() as session:
            rows = list((await session.execute(query)).scalars())
        page = rows[:limit]
        return RunPage(page, encode_cursor(page[-1]) if len(rows) > limit else None)

    async def list_views(self, user_id: str, *, limit: int, cursor: str | None) -> tuple[list[RunView], str | None]:
        """One page of runs as the client sees them, with each run's payment and revised delivery date.
        Both are loaded for the whole page in one query each."""
        page = await self.list(user_id, limit=limit, cursor=cursor)
        run_ids = [r.run_id for r in page.items]
        async with self._factory() as session:
            payments = await self._latest_payments(session, run_ids)
            expected = await self._expected_by(session, run_ids)
        views = [run_view(r, payment=payments.get(r.run_id), expected_by=expected.get(r.run_id)) for r in page.items]
        return views, page.next_cursor

    @staticmethod
    async def _latest_payments(session: AsyncSession, run_ids: list[str]) -> dict[str, Payment]:
        if not run_ids:
            return {}
        rows = (
            await session.execute(select(Payment).where(Payment.run_id.in_(run_ids)).order_by(Payment.created_at))
        ).scalars()
        return {p.run_id: p for p in rows}  # ascending order: the newest payment wins

    @staticmethod
    async def _expected_by(session: AsyncSession, run_ids: list[str]) -> dict[str, datetime]:
        """The counterparty's most recent revised delivery date per run (from applied deadline-risk events)."""
        if not run_ids:
            return {}
        rows = (
            await session.execute(
                select(MonitorEventRecord)
                .where(
                    MonitorEventRecord.run_id.in_(run_ids),
                    MonitorEventRecord.status == "applied",
                    MonitorEventRecord.problem == Problem.DEADLINE_RISK.value,
                )
                .order_by(MonitorEventRecord.occurred_at, MonitorEventRecord.rank)
            )
        ).scalars()
        result: dict[str, datetime] = {}
        for row in rows:  # ascending: the latest event wins
            try:
                result[row.run_id] = parse_instant((row.payload or {}).get("new_date"))
            except InvalidEvent:
                logger.warning("deadline event has no valid new_date", extra={"run_id": row.run_id})
        return result

    async def aclose(self) -> None:
        """Wait for in-flight starts so a shutdown does not abandon a run mid-step (it resumes from its checkpoint)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- full view, chat, payer return, live stream ------------------------------------------------

    async def view(self, user_id: str, run_id: str) -> RunView | None:
        """The run as the client sees it, from checkpointed state. Also stores a newly asked question and keeps the
        index status current."""
        record = await self.get(user_id, run_id)
        if record is None:
            return None
        try:
            snapshot: RunSnapshot | None = await self._runs.snapshot(run_id)
        except UnknownRun:
            snapshot = None  # created, graph not checkpointed yet
        if snapshot is not None:
            await self._store_question(run_id, snapshot)
        async with self._factory() as session:
            query = select(RunMessage).where(RunMessage.run_id == run_id).order_by(RunMessage.created_at, RunMessage.id)
            messages = list((await session.execute(query)).scalars())
            payment_id = snapshot.values.get("payment_id") if snapshot else None
            payment = await session.get(Payment, UUID(payment_id)) if payment_id else None
            expected_by = (await self._expected_by(session, [run_id])).get(run_id)
            milestones = list((await session.execute(select(Milestone).where(Milestone.run_id == run_id))).scalars())
        view = run_view(record, snapshot, payment=payment, messages=messages, expected_by=expected_by, milestones=milestones)
        if snapshot is not None and view.status != record.status:
            await self._set_status(run_id, view.status)
        return view

    async def post_message(self, user_id: str, run_id: str, text: str, client_id: str) -> MessageView | None:
        """Answer the run's question. Idempotent per ``client_id``; the graph resumes in the background."""
        if await self.get(user_id, run_id) is None:
            return None
        message_id = f"u-{client_id}"
        async with self._factory() as session:
            existing = await session.get(RunMessage, (run_id, message_id))
        if existing is not None:
            return message_view(existing)  # a retry: already accepted
        snapshot = await self._runs.snapshot(run_id)
        answering = bool(QUESTION_NODES & set(snapshot.next))
        if run_id in self._resuming or not (answering or can_revise(snapshot)):
            raise NotAwaiting("the run is not waiting for an answer")
        row = RunMessage(run_id=run_id, id=message_id, role="user", text=text, client_id=client_id)
        async with session_scope(self._factory) as session:
            session.add(row)
        # A stopped run has nothing to resume: the message re-opens it with the changed requirements.
        self._resume_in_background(run_id, text, undo_message=message_id, revise=not answering)
        return message_view(row)

    async def payer_return(self, user_id: str, run_id: str) -> bool:
        """The payer came back from PayPal: resume the run, which verifies the order itself. False if not found."""
        if await self.get(user_id, run_id) is None:
            return False
        if run_id in self._resuming:
            return True  # already resuming; a repeated call is a no-op
        snapshot = await self._runs.snapshot(run_id)
        if "await_payer" not in snapshot.next:
            raise NotAwaiting("the run is not waiting for the payer")
        self._resume_in_background(run_id, {"payer_returned": True})
        return True

    async def ensure_awaiting_approval(self, run_id: str) -> None:
        """Raise ``RunBusy`` / ``NotAwaiting`` unless the run is paused at the approval gate and nothing is resuming it."""
        if run_id in self._resuming:
            raise RunBusy(run_id)
        if "await_approval" not in (await self._runs.snapshot(run_id)).next:
            raise NotAwaiting("the run is not waiting for an approval")

    async def resume_approval(self, run_id: str, value: object) -> TaskState:
        """Resume a run paused at ``await_approval`` with the user's decision, inline (the caller returns the status).
        Holds the same guard as every other resume, so a double-submitted decision cannot run the gate twice."""
        await self.ensure_awaiting_approval(run_id)
        self._resuming.add(run_id)
        try:
            return await self._runs.resume(run_id, value)
        finally:
            self._resuming.discard(run_id)

    async def cancel(self, user_id: str, run_id: str) -> bool:
        """Cancel a run that is paused (asking, awaiting approval, or awaiting the payer). Committed funds are released
        first; if that is not possible the run stays open. False if not found; raises ``RunBusy`` / ``CannotCancel``."""
        if await self.get(user_id, run_id) is None:
            return False
        if run_id in self._resuming:
            raise RunBusy(run_id)
        try:
            snapshot = await self._runs.snapshot(run_id)
        except UnknownRun:
            raise RunBusy(run_id) from None  # not checkpointed yet: the graph is still starting
        if derive_status(snapshot) in TERMINAL_STATUSES and not snapshot.next:
            raise CannotCancel("the run has already finished")
        if self._release_funds is not None and not await self._release_funds(run_id):
            raise CannotCancel("committed funds could not be released")
        await self._runs.cancel(run_id)
        await self._set_status(run_id, "stopped")
        return True

    async def events(self, user_id: str, run_id: str) -> AsyncIterator[str | None]:
        """Server-sent events: a ``run.snapshot`` frame now and whenever the view changes; ``None`` is a keep-alive
        tick. A snapshot per change keeps the stream stateless, so a reconnect needs no replay."""
        last: str | None = None
        idle = 0.0
        while True:
            view = await self.view(user_id, run_id)
            if view is None:
                return
            payload = view.model_dump_json(exclude_none=True)  # absent, not null: the client types use optional fields
            if payload != last:
                last, idle = payload, 0.0
                yield f'event: run.snapshot\ndata: {{"run": {payload}}}\n\n'
            elif idle >= _HEARTBEAT_SECONDS:
                idle = 0.0
                yield None
            await asyncio.sleep(_EVENT_POLL_SECONDS)
            idle += _EVENT_POLL_SECONDS

    async def _store_question(self, run_id: str, snapshot: RunSnapshot) -> None:
        text = question_text(snapshot)
        if text is None:
            return
        digest = hashlib.sha256(f"{run_id}\n{text}".encode()).hexdigest()[:16]
        row = RunMessage(run_id=run_id, id=f"q-{digest}", role="agent", agent="coordinator", text=text, is_question=True)
        try:
            async with session_scope(self._factory) as session:
                if await session.get(RunMessage, (run_id, row.id)) is None:
                    session.add(row)
        except IntegrityError:
            pass  # a concurrent observer stored the same question

    async def _set_status(self, run_id: str, status: str) -> None:
        # Never touches ``_resuming``: only the task that runs the graph may release that guard (B2).
        async with session_scope(self._factory) as session:
            record = await session.get(RunRecord, run_id)
            if record is not None:
                record.status = status

    def _resume_in_background(
        self, run_id: str, value: object, *, undo_message: str | None = None, revise: bool = False
    ) -> None:
        self._resuming.add(run_id)
        task = asyncio.create_task(self._resume(run_id, value, undo_message, revise), name=f"resume-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resume(self, run_id: str, value: object, undo_message: str | None, revise: bool) -> None:
        try:
            await (self._runs.revise(run_id, str(value)) if revise else self._runs.resume(run_id, value))
        except Exception:
            logger.exception("run failed to resume", extra={"run_id": run_id})
            if undo_message is not None:  # the checkpoint is unchanged, so the user can send the answer again
                async with session_scope(self._factory) as session:
                    stale = await session.get(RunMessage, (run_id, undo_message))
                    if stale is not None:
                        await session.delete(stale)
        finally:
            self._resuming.discard(run_id)

    # ---- internals ---------------------------------------------------------------------------------

    async def _by_key(self, user_id: str, key: str) -> RunRecord | None:
        async with self._factory() as session:
            return (
                await session.execute(
                    select(RunRecord).where(RunRecord.user_id == user_id, RunRecord.idempotency_key == key)
                )
            ).scalar_one_or_none()

    def _spawn(self, record: RunRecord, new: NewRun) -> None:
        self._resuming.add(record.run_id)
        task = asyncio.create_task(self._start(record.run_id, new), name=f"run-{record.run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self, run_id: str, new: NewRun) -> None:
        authorization = TaskAuthorization(
            run_id=run_id, max_spend=new.max_spend, currency=new.currency, permitted_platforms=self._platforms,
            permitted_actions=granted_actions(new.domain), expires_at=datetime.now(UTC) + _AUTHORIZATION_TTL,
        )
        initial = make_initial_state(
            run_id=run_id, original_intent=intent_text(new), authorization=authorization, domain=new.domain,
            deliver_by=deadline_of(new.deliver_by), counterparty=new.counterparty,
        )
        status = "failed"
        try:
            final = await self._runs.start(initial)
            status = final.get("status", "running")
        except Exception as exc:
            logger.exception("run failed to start", extra={"run_id": run_id})
            await self._record_start_failure(run_id, exc)
        finally:
            self._resuming.discard(run_id)  # the graph is paused or finished: outside actions may proceed
        async with session_scope(self._factory) as session:
            record = await session.get(RunRecord, run_id)
            if record is not None:
                record.status = status

    async def _record_start_failure(self, run_id: str, exc: Exception) -> None:
        """Make the checkpoint agree with the record: a crashed first invocation is ``failed``, never still running.
        No money exists before the first invocation pauses, so ending the run here cannot strand funds."""
        reason = f"the run stopped unexpectedly ({type(exc).__name__}); please start it again"
        try:
            await self._runs.fail(run_id, reason)
        except UnknownRun:
            pass  # nothing was checkpointed: the record alone says failed
        except Exception:
            logger.exception("could not record the run failure in its checkpoint", extra={"run_id": run_id})
