"""Run lifecycle for the API: create (idempotent), list, get. The graph runs in a background task so a request
never waits on LLM/connector calls; the run index row is the source of truth for ownership and idempotency."""

import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models.run import RunRecord
from app.db.session import session_scope
from app.orchestrator.graph.runtime import RunController
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.state import make_initial_state

logger = get_logger(__name__)

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
