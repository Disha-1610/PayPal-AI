"""Run lifecycle for the API: create (idempotent), list, get. The graph runs in a background task so a request
never waits on LLM/connector calls; the run index row is the source of truth for ownership and idempotency."""

import asyncio
import base64
import binascii
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.payment import Payment
from app.db.models.run import RunRecord
from app.db.models.run_message import RunMessage
from app.db.session import session_scope
from app.orchestrator.graph.runtime import RunController, RunSnapshot, UnknownRun
from app.runs.view import MessageView, RunView, message_view, question_text, run_view
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.state import make_initial_state

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
_AUTHORIZATION_TTL = timedelta(days=7)


class InvalidCursor(ValueError):
    """The pagination cursor was not issued by this service."""


class NotAwaiting(Exception):
    """The run is not paused where this action applies (not asking a question / not waiting for the payer)."""


@dataclass(frozen=True)
class NewRun:
    goal: str
    max_spend: Decimal
    currency: str
    deliver_by: datetime | None = None


@dataclass(frozen=True)
class RunPage:
    items: list[RunRecord]
    next_cursor: str | None


def intent_text(new: NewRun) -> str:
    """The text the Intent agent extracts constraints from: the goal plus the structured intake fields."""
    parts = [new.goal.strip(), f"Budget: {new.max_spend} {new.currency}."]
    if new.deliver_by is not None:
        parts.append(f"Must be delivered by {new.deliver_by.date().isoformat()}.")
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
    ) -> None:
        self._runs, self._factory, self._platforms = runs, session_factory, platforms
        self._tasks: set[asyncio.Task[None]] = set()
        self._resuming: set[str] = set()  # runs with a resume in flight: a second one would re-run the paused node

    async def create(self, user_id: str, idempotency_key: str, new: NewRun) -> RunRecord:
        """Create the run and start it in the background. A repeated key returns the original run."""
        existing = await self._by_key(user_id, idempotency_key)
        if existing is not None:
            return existing
        record = RunRecord(
            run_id=uuid4().hex, user_id=user_id, idempotency_key=idempotency_key, goal=new.goal.strip(),
            max_spend=new.max_spend, currency=new.currency, deliver_by=new.deliver_by, status="created",
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
        view = run_view(record, snapshot, payment=payment, messages=messages)
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
        if "ask_user" not in snapshot.next or run_id in self._resuming:
            raise NotAwaiting("the run is not waiting for an answer")
        row = RunMessage(run_id=run_id, id=message_id, role="user", text=text, client_id=client_id)
        async with session_scope(self._factory) as session:
            session.add(row)
        self._resume_in_background(run_id, text, undo_message=message_id)
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

    async def events(self, user_id: str, run_id: str) -> AsyncIterator[str | None]:
        """Server-sent events: a ``run.snapshot`` frame now and whenever the view changes; ``None`` is a keep-alive
        tick. A snapshot per change keeps the stream stateless, so a reconnect needs no replay."""
        last: str | None = None
        idle = 0.0
        while True:
            view = await self.view(user_id, run_id)
            if view is None:
                return
            payload = view.model_dump_json()
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
        async with session_scope(self._factory) as session:
            record = await session.get(RunRecord, run_id)
            if record is not None:
                record.status = status

    def _resume_in_background(self, run_id: str, value: object, *, undo_message: str | None = None) -> None:
        self._resuming.add(run_id)
        task = asyncio.create_task(self._resume(run_id, value, undo_message), name=f"resume-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resume(self, run_id: str, value: object, undo_message: str | None) -> None:
        try:
            await self._runs.resume(run_id, value)
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
        task = asyncio.create_task(self._start(record.run_id, new), name=f"run-{record.run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _start(self, run_id: str, new: NewRun) -> None:
        authorization = TaskAuthorization(
            run_id=run_id, max_spend=new.max_spend, currency=new.currency, permitted_platforms=self._platforms,
            permitted_actions=_GRANTED_ACTIONS, expires_at=datetime.now(UTC) + _AUTHORIZATION_TTL,
        )
        initial = make_initial_state(run_id=run_id, original_intent=intent_text(new), authorization=authorization)
        status = "failed"
        try:
            final = await self._runs.start(initial)
            status = final.get("status", "running")
        except Exception:
            logger.exception("run failed to start", extra={"run_id": run_id})
        async with session_scope(self._factory) as session:
            record = await session.get(RunRecord, run_id)
            if record is not None:
                record.status = status
