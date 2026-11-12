"""The outreach desk: talks to the counterparty the user named and brings its replies back into the run.

Out:  ``request_quote`` (the graph's ``QuoteChannel``) and ``send_counter`` (its ``CounterpartyChannel``) send one
      message on every channel the contact allows, record a reply deadline, and return. The run then parks at an
      interrupt (``await_quote`` / ``await_counterparty``); nothing waits in memory, so a reply may come days later.
In:   ``apply_reply`` takes a reply that a route has already authenticated (signed link, Twilio signature, inbound
      secret, agent callback token), checks it answers the round still open, and resumes the run through
      ``RunController`` (sanitized, then Guard -> Negotiator / Evaluator).
Time: ``sweep`` sends one reminder when the deadline passes, then resumes the run with a timeout so the user decides.

Never sent to a counterparty: the user's budget, price ceiling, identity or any other run's data.
"""

import asyncio
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.counterparty.tokens import ReplyClaim, ReplyTokens, Stage
from app.counterparty.transports import (
    AgentClient,
    EmailSender,
    TextSender,
    TransportError,
)
from app.db.models.run_counterparty import RunCounterparty
from app.db.models.run_message import RunMessage
from app.db.session import session_scope
from app.orchestrator.graph.negotiation import ChannelError
from app.orchestrator.graph.quotes import QuoteRequest
from app.orchestrator.graph.runtime import NotNegotiable, RunController, UnknownRun
from app.schemas.approval_terms import from_minor_units, to_minor_units
from app.schemas.counterparty import CounterpartyContact
from app.schemas.negotiator import CounterOffer

logger = get_logger(__name__)

AWAIT_NODE: dict[Stage, str] = {"quote": "await_quote", "counter": "await_counterparty"}
TIMEOUT_REPLY = {"decision": "timeout"}
_PAUSE_WAIT_SECONDS = 30.0
_PAUSE_POLL_SECONDS = 0.25


class OutreachFailed(ChannelError):
    """No channel could deliver the message. The text is safe to show the user."""


class StaleReply(Exception):
    """The reply answers a round that is no longer open (already answered, timed out, or superseded)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _money(minor: int, currency: str) -> str:
    return f"{from_minor_units(minor, currency):.2f} {currency}"


class CounterpartyDesk:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tokens: ReplyTokens,
        *,
        public_base_url: str,
        email: EmailSender | None = None,
        text: TextSender | None = None,
        agents: AgentClient | None = None,
        reply_window: timedelta = timedelta(hours=48),
        grace: timedelta = timedelta(hours=24),
        clock: Any = _now,
    ) -> None:
        self._factory, self._tokens = session_factory, tokens
        self._base = public_base_url.rstrip("/")
        self._email, self._text, self._agents = email, text, agents
        self._window, self._grace, self._clock = reply_window, grace, clock
        self._runs: RunController | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, runs: RunController) -> None:
        self._runs = runs

    def verify_token(self, token: str) -> ReplyClaim:
        return self._tokens.verify(token)

    def public_url(self, path: str) -> str:
        """The URL a provider called, as it saw it (Twilio signs the public URL, not the one behind a proxy)."""
        return f"{self._base}{path}"

    async def form_payload(self, claim: ReplyClaim, form: Mapping[str, str]) -> dict[str, object]:
        """The reply page's form as a reply payload. Raises ``ValueError`` for an unreadable price or date."""
        decision = form.get("decision", "")
        if decision == "decline":
            return {"decision": "decline"}
        row = await self._row(claim.run_id)
        offer = (row.last_offer if row is not None else None) or {}
        if decision == "accept":
            if claim.stage != "counter" or not offer:
                raise ValueError("nothing to accept")
            return dict(offer)
        if decision != "quote":
            raise ValueError("unknown decision")
        text = f"QUOTE {form.get('price', '').strip()} {form.get('delivery_due', '').strip()}"
        payload = parse_text_reply(text, currency=_row_currency(row) if row is not None else "USD", last_offer=offer or None)
        if payload is None:
            raise ValueError("unreadable price or date")
        return payload

    def reply_link(self, claim: ReplyClaim) -> str:
        return f"{self._base}/counterparty/reply/{self._tokens.issue(claim, now=self._clock())}"

    def callback_url(self, claim: ReplyClaim) -> str:
        return f"{self._base}/a2a/callback/{self._tokens.issue(claim, now=self._clock())}"

    # ---- outbound ---------------------------------------------------------------------------------

    async def request_quote(self, request: QuoteRequest) -> None:
        """Ask the counterparty for a price and delivery date. Raises ``OutreachFailed`` if no channel delivered."""
        claim = ReplyClaim(run_id=request.run_id, stage="quote", round=request.round)
        due = f" It must be finished by {request.deliver_by.date().isoformat()}." if request.deliver_by else ""
        body = (
            f"Hello {request.contact.name},\n\nI am an assistant acting for a client who would like a quote for this work:\n\n"
            f"{request.goal}\n\nPrices are in {request.currency}.{due}\n\n"
            f"Please send your price and the date you can deliver by here:\n{self.reply_link(claim)}\n\n"
            "You can also reply to this message with: QUOTE <amount> <YYYY-MM-DD>, or DECLINE."
        )
        payload = {
            "type": "quote_request", "goal": request.goal, "currency": request.currency,
            "deliver_by": request.deliver_by.isoformat() if request.deliver_by else None,
            "callback_url": self.callback_url(claim),
        }
        await self._upsert(request.run_id, request.contact, stage="quote", round_=request.round, last_offer=None)
        await self._dispatch(request.run_id, request.contact, claim, "Quote request", body, payload)

    async def send_counter(self, run_id: str, offer: CounterOffer) -> None:
        """``CounterpartyChannel``: our Guard-checked counter-offer to the counterparty."""
        row = await self._row(run_id)
        if row is None:
            raise OutreachFailed("no counterparty is on record for this run")
        contact = CounterpartyContact.model_validate(row.contact)
        claim = ReplyClaim(run_id=run_id, stage="counter", round=row.round + 1)
        body = (
            f"Hello {contact.name},\n\nMy client proposes {_money(offer.price_minor, offer.currency)}, delivered by "
            f"{offer.delivery_due.date().isoformat()}.\n\nAccept, decline or send a new price here:\n{self.reply_link(claim)}\n\n"
            "Or reply with: ACCEPT, DECLINE, or QUOTE <amount> <YYYY-MM-DD>."
        )
        offer_json = offer.model_dump(mode="json")
        payload = {"type": "counter_offer", "offer": offer_json, "callback_url": self.callback_url(claim)}
        await self._upsert(run_id, contact, stage="counter", round_=claim.round, last_offer=offer_json)
        await self._dispatch(run_id, contact, claim, "Counter-offer", body, payload)

    async def _dispatch(
        self, run_id: str, contact: CounterpartyContact, claim: ReplyClaim, subject: str, body: str,
        agent_payload: Mapping[str, object],
    ) -> None:
        sent: list[str] = []
        failures: list[str] = []
        if contact.agent_endpoint and self._agents is not None:
            try:
                reply = await self._agents.post(contact.agent_endpoint, agent_payload)
                sent.append("the agent's endpoint")
                if reply is not None:  # answered inline: deliver once the run is parked for it
                    self._later(self._deliver_inline(claim, reply))
            except TransportError as exc:
                failures.append(f"agent: {exc}")
        for channel in contact.reachable_channels:
            try:
                if channel == "email" and contact.email:
                    if self._email is None:
                        raise TransportError("email is not configured")
                    await self._email.send_email(contact.email, f"{subject} for {contact.name}", body)
                elif channel in ("sms", "whatsapp") and contact.phone:
                    if self._text is None:
                        raise TransportError("text messaging is not configured")
                    await self._text.send_text(contact.phone, body, whatsapp=channel == "whatsapp")
                else:
                    continue
                sent.append(channel)
            except TransportError as exc:
                failures.append(f"{channel}: {exc}")
        if failures:
            logger.warning("some counterparty channels failed", extra={"run_id": run_id, "failures": failures})
        if not sent:
            await self._set_awaiting(run_id, False)
            raise OutreachFailed(f"could not reach {contact.name} ({'; '.join(failures) or 'no usable channel'})")
        await self._note(run_id, f"{claim.stage}-{claim.round}-sent", f"{subject} sent to {contact.name} by {', '.join(sent)}. Waiting for their reply.")

    # ---- inbound ----------------------------------------------------------------------------------

    async def apply_reply(self, claim: ReplyClaim, payload: Mapping[str, object]) -> None:
        """Resume the run with an authenticated reply. Raises ``StaleReply`` unless that exact round is still open."""
        runs = self._require_runs()
        if not await self._close_round(claim):
            raise StaleReply("this reply is for a request that is no longer open")
        try:
            await self._resume(runs, claim.run_id, claim.stage, dict(payload))
        except StaleReply:
            raise  # the run moved on (cancelled, answered elsewhere): the round stays closed
        except Exception:
            await self._reopen(claim)  # the checkpoint did not move: the counterparty may send again
            raise

    async def reply_from_text(self, *, phone: str | None = None, email: str | None = None, text: str) -> bool:
        """An SMS / email reply. A recognised command answers the open round; anything else is passed to the user's
        chat as a note. False when no run is waiting on this sender."""
        row = await self._open_row_for(phone=phone, email=email)
        if row is None:
            return False
        claim = ReplyClaim(run_id=row.run_id, stage=row.stage, round=row.round)  # type: ignore[arg-type]
        payload = parse_text_reply(text, currency=_row_currency(row), last_offer=row.last_offer)
        if payload is None:
            await self._note(row.run_id, "in-" + hashlib.sha256(f"{row.round}|{text}".encode()).hexdigest()[:20], f"Message from the counterparty: {text[:1000]}", role="system")
            return True
        await self.apply_reply(claim, payload)
        return True

    async def has_counterparty(self, run_id: str) -> bool:
        return await self._row(run_id) is not None

    async def open_round(self, run_id: str) -> RunCounterparty | None:
        row = await self._row(run_id)
        return row if row is not None and row.awaiting else None

    # ---- time -------------------------------------------------------------------------------------

    async def sweep(self) -> int:
        """Remind once at the deadline; after the grace period, hand the decision to the user. Returns rows handled."""
        now = self._clock()
        async with self._factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(RunCounterparty).where(RunCounterparty.awaiting.is_(True), RunCounterparty.reply_deadline <= now)
                    )
                ).scalars()
            )
        handled = 0
        for row in rows:
            try:
                if not await self._still_waiting(row):
                    await self._close_round(ReplyClaim(run_id=row.run_id, stage=row.stage, round=row.round))  # type: ignore[arg-type]
                elif not row.reminder_sent:
                    await self._remind(row)
                else:
                    await self._time_out(row)
                handled += 1
            except Exception:
                logger.exception("counterparty sweep failed for a run", extra={"run_id": row.run_id})
        return handled

    async def _still_waiting(self, row: RunCounterparty) -> bool:
        """False once the run left the wait (cancelled, failed, answered elsewhere): nobody is reminded for it."""
        try:
            snapshot = await self._require_runs().snapshot(row.run_id)
        except UnknownRun:
            return False
        return AWAIT_NODE.get(row.stage, "") in snapshot.next  # type: ignore[call-overload]

    async def _remind(self, row: RunCounterparty) -> None:
        contact = CounterpartyContact.model_validate(row.contact)
        claim = ReplyClaim(run_id=row.run_id, stage=row.stage, round=row.round)  # type: ignore[arg-type]
        body = f"Hello {contact.name},\n\nA reminder about our earlier request. You can reply here:\n{self.reply_link(claim)}"
        try:
            await self._dispatch(row.run_id, contact, claim, "Reminder", body, {"type": "reminder", "callback_url": self.callback_url(claim)})
        except OutreachFailed:
            logger.warning("reminder could not be delivered", extra={"run_id": row.run_id})
        async with session_scope(self._factory) as session:
            fresh = await session.get(RunCounterparty, row.run_id)
            if fresh is not None and fresh.awaiting and fresh.round == row.round:
                fresh.reminder_sent = True
                fresh.reply_deadline = self._clock() + self._grace

    async def _time_out(self, row: RunCounterparty) -> None:
        claim = ReplyClaim(run_id=row.run_id, stage=row.stage, round=row.round)  # type: ignore[arg-type]
        if not await self._close_round(claim):
            return
        try:
            await self._resume(self._require_runs(), row.run_id, claim.stage, dict(TIMEOUT_REPLY))
        except (UnknownRun, StaleReply):
            return  # the run is no longer waiting: the round stays closed
        except Exception:
            await self._reopen(claim)
            raise

    # ---- internals --------------------------------------------------------------------------------

    def _require_runs(self) -> RunController:
        if self._runs is None:
            raise RuntimeError("the counterparty desk is not bound to the run controller")
        return self._runs

    async def _resume(self, runs: RunController, run_id: str, stage: Stage, payload: dict[str, object]) -> None:
        snapshot = await runs.snapshot(run_id)
        node = AWAIT_NODE[stage]
        if node not in snapshot.next:
            raise StaleReply("the run is not waiting for this reply")
        if stage == "counter" and payload.get("decision") not in ("decline", "timeout"):
            try:
                await runs.receive_counter_offer(run_id, payload)
                return
            except NotNegotiable as exc:
                raise StaleReply("the run is not waiting for this reply") from exc
        await runs.resume(run_id, payload)

    async def _deliver_inline(self, claim: ReplyClaim, reply: dict[str, Any]) -> None:
        runs = self._require_runs()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PAUSE_WAIT_SECONDS
        try:
            while AWAIT_NODE[claim.stage] not in (await runs.snapshot(claim.run_id)).next:
                if loop.time() > deadline:
                    logger.warning("run never paused for the agent's inline reply", extra={"run_id": claim.run_id})
                    return
                await asyncio.sleep(_PAUSE_POLL_SECONDS)
            await self.apply_reply(claim, reply)
        except StaleReply:
            logger.info("inline agent reply arrived for a closed round", extra={"run_id": claim.run_id})
        except Exception:
            logger.exception("inline agent reply failed", extra={"run_id": claim.run_id})

    def _later(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _row(self, run_id: str) -> RunCounterparty | None:
        async with self._factory() as session:
            return await session.get(RunCounterparty, run_id)

    async def _open_row_for(self, *, phone: str | None, email: str | None) -> RunCounterparty | None:
        if not phone and not email:
            return None
        column = RunCounterparty.phone if phone else RunCounterparty.email
        value = phone if phone else (email or "").lower()
        async with self._factory() as session:
            rows = (
                await session.execute(
                    select(RunCounterparty).where(column == value, RunCounterparty.awaiting.is_(True)).order_by(RunCounterparty.updated_at.desc())
                )
            ).scalars().all()
        if len(rows) > 1:
            logger.warning("sender has several open runs; replying to the newest", extra={"count": len(rows)})
        return rows[0] if rows else None

    async def _upsert(
        self, run_id: str, contact: CounterpartyContact, *, stage: Stage, round_: int, last_offer: dict[str, object] | None
    ) -> None:
        now = self._clock()
        async with session_scope(self._factory) as session:
            row = await session.get(RunCounterparty, run_id)
            if row is None:
                row = RunCounterparty(run_id=run_id, contact={}, stage=stage)
                session.add(row)
            row.contact = contact.model_dump(mode="json")
            row.email, row.phone = contact.email, contact.phone
            row.stage, row.round, row.last_offer = stage, round_, last_offer
            row.awaiting, row.reminder_sent = True, False
            row.reply_deadline, row.last_contacted_at = now + self._window, now

    async def _set_awaiting(self, run_id: str, awaiting: bool) -> None:
        async with session_scope(self._factory) as session:
            row = await session.get(RunCounterparty, run_id)
            if row is not None:
                row.awaiting = awaiting

    async def _close_round(self, claim: ReplyClaim) -> bool:
        """Atomically take the open round: only the first reply (or the timeout) for it proceeds."""
        async with session_scope(self._factory) as session:
            row = await session.get(RunCounterparty, claim.run_id, with_for_update=True)
            if row is None or not row.awaiting or row.stage != claim.stage or row.round != claim.round:
                return False
            row.awaiting = False
            return True

    async def _reopen(self, claim: ReplyClaim) -> None:
        async with session_scope(self._factory) as session:
            row = await session.get(RunCounterparty, claim.run_id)
            if row is not None and row.stage == claim.stage and row.round == claim.round:
                row.awaiting = True

    async def _note(self, run_id: str, key: str, text: str, *, role: str = "agent") -> None:
        try:
            async with session_scope(self._factory) as session:
                message_id = f"cp-{key}"[:128]
                if await session.get(RunMessage, (run_id, message_id)) is None:
                    session.add(RunMessage(run_id=run_id, id=message_id, role=role, agent="negotiator", text=text))
        except Exception:
            logger.exception("could not record the outreach note", extra={"run_id": run_id})

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for transport in (self._text, self._agents):
            close = getattr(transport, "aclose", None)
            if close is not None:
                await close()


def _row_currency(row: RunCounterparty) -> str:
    offer = row.last_offer or {}
    currency = offer.get("currency")
    return currency if isinstance(currency, str) else "USD"


def parse_text_reply(text: str, *, currency: str, last_offer: Mapping[str, object] | None) -> dict[str, object] | None:
    """A strict command from an SMS / email body; None when the text is not one (it is then shown to the user, never
    interpreted). ``QUOTE <amount> <YYYY-MM-DD>`` | ``ACCEPT`` (our last counter-offer) | ``DECLINE``."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    words = lines[0].split()
    command = words[0].upper().rstrip(".!,")
    if command == "DECLINE" and len(words) == 1:
        return {"decision": "decline"}
    if command == "ACCEPT" and len(words) == 1 and last_offer:
        return dict(last_offer)
    if command == "QUOTE" and len(words) == 3:
        try:
            amount = Decimal(words[1].lstrip("$").replace(",", ""))
            due = datetime.fromisoformat(words[2]).replace(tzinfo=UTC)
        except (ArithmeticError, ValueError):
            return None
        reply: dict[str, object] = {"price": str(amount), "currency": currency, "delivery_due": due.isoformat()}
        if last_offer:  # a counter in the negotiation keeps the item and parties of our offer
            reply.update({k: last_offer[k] for k in ("subject_id", "counterparty_id") if k in last_offer})
            reply["price_minor"] = to_minor_units(amount, currency)
        return reply
    return None

