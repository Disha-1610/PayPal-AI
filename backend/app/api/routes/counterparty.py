"""Counterparty replies and delivery confirmation.

Every inbound path authenticates before anything is read into a run:
* ``/counterparty/reply/{token}``  the reply page linked in emails and texts (signed, expiring, round-bound token)
* ``/a2a/callback/{token}``        an agent's asynchronous reply (same token)
* ``/webhooks/twilio``             SMS / WhatsApp replies (``X-Twilio-Signature``)
* ``/webhooks/email-inbound``      email replies from an inbound-mail provider (shared secret header)
Replies are handed to ``CounterpartyDesk.apply_reply``, which resumes the run through ``RunController`` (sanitized,
then Guard). ``/runs/{id}/confirm-delivery`` is the user's own confirmation that a named counterparty delivered.
"""

import hmac
import re
from datetime import UTC, datetime
from html import escape
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import (
    MonitorDispatch,
    Principal,
    get_counterparty_desk,
    get_inbound_secret,
    get_monitor_dispatch,
    get_monitor_processor,
    get_principal,
    get_run_service,
    get_runs,
    get_session_factory,
    get_twilio_auth_token,
)
from app.core.logging import get_logger
from app.counterparty.desk import CounterpartyDesk, StaleReply
from app.counterparty.tokens import InvalidReplyToken, ReplyClaim
from app.counterparty.transports import twilio_signature_valid
from app.db.session import session_scope
from app.domains import get_plugin
from app.monitor.events.processor import EventProcessor
from app.orchestrator.graph.runtime import RunController
from app.runs.service import RunService
from app.schemas.approval_terms import from_minor_units
from app.schemas.counterparty import normalize_phone
from app.schemas.monitor import EventOutcome, MonitorEvent, Problem

router = APIRouter(tags=["counterparty"])
logger = get_logger(__name__)

_MAX_FORM_BYTES = 8 * 1024
_MAX_INBOUND_BYTES = 256 * 1024
_TOKEN_IN_TEXT = re.compile(r"/counterparty/reply/([A-Za-z0-9_\-.]{20,2048})")
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reply to request</title><style>body{{font:16px system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
label{{display:block;margin:.8rem 0 .3rem}}input{{font:inherit;padding:.4rem;width:100%;box-sizing:border-box}}
button{{font:inherit;padding:.5rem 1rem;margin:.8rem .5rem 0 0}}.note{{color:#555}}</style></head><body>{body}</body></html>"""


def _claim(desk: CounterpartyDesk, token: str) -> ReplyClaim:
    try:
        return desk.verify_token(token)
    except InvalidReplyToken as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this link is invalid or has expired") from exc


def _page(body: str, code: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(body=body), status_code=code, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/counterparty/reply/{token}", response_class=HTMLResponse)
async def reply_page(token: str, desk: Annotated[CounterpartyDesk, Depends(get_counterparty_desk)]) -> HTMLResponse:
    claim = _claim(desk, token)
    row = await desk.open_round(claim.run_id)
    if row is None or row.stage != claim.stage or row.round != claim.round:
        return _page("<h1>Thanks</h1><p>This request is no longer open.</p>", 410)
    action = f"/counterparty/reply/{escape(token, quote=True)}"
    if claim.stage == "quote":
        intro = "<h1>Send your quote</h1><p class=note>Give your total price and the date you can finish by.</p>"
    else:
        offer = row.last_offer or {}
        currency, minor = str(offer.get("currency", "USD")), offer.get("price_minor")
        price = f"{from_minor_units(minor, currency):.2f} {currency}" if isinstance(minor, int) else "?"
        due = str(offer.get("delivery_due", ""))[:10]
        intro = (
            f"<h1>Counter-offer</h1><p>Proposed: <b>{escape(price)}</b>, delivered by <b>{escape(due)}</b>.</p><form method=post action='{action}'><input type=hidden name=decision value=accept>"
            "<button>Accept</button></form><p class=note>Or propose another price:</p>"
        )
    form = (
        f"<form method=post action='{action}'><input type=hidden name=decision value=quote>"
        "<label for=price>Total price (USD)</label><input id=price name=price inputmode=decimal required pattern='[0-9]+(\\.[0-9]{1,2})?'>"
        "<label for=due>Delivery date</label><input id=due name=delivery_due type=date required>"
        "<button>Send</button></form>"
        f"<form method=post action='{action}'><input type=hidden name=decision value=decline><button>Decline</button></form>"
    )
    return _page(intro + form)


@router.post("/counterparty/reply/{token}", response_class=HTMLResponse)
async def submit_reply(
    token: str, request: Request, desk: Annotated[CounterpartyDesk, Depends(get_counterparty_desk)]
) -> HTMLResponse:
    claim = _claim(desk, token)
    raw = await request.body()
    if len(raw) > _MAX_FORM_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), max_num_fields=8).items()}
    try:
        payload = await desk.form_payload(claim, form)
    except (ValueError, ValidationError):
        return _page("<h1>Check your reply</h1><p>Please enter a price like 1200.00 and a valid date.</p>", 422)
    try:
        await desk.apply_reply(claim, payload)
    except StaleReply:
        return _page("<h1>Thanks</h1><p>This request is no longer open.</p>", 410)
    except ValidationError:
        return _page("<h1>Check your reply</h1><p>The offer could not be read.</p>", 422)
    return _page("<h1>Thank you</h1><p>Your reply was sent.</p>")


@router.post("/a2a/callback/{token}")
async def agent_callback(
    token: str, request: Request, desk: Annotated[CounterpartyDesk, Depends(get_counterparty_desk)]
) -> dict[str, str]:
    claim = _claim(desk, token)
    if len(await request.body()) > _MAX_FORM_BYTES * 8:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
    try:
        body: Any = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not an object")
    try:
        await desk.apply_reply(claim, body)
    except StaleReply as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "this request is no longer open") from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "the offer is invalid") from exc
    return {"status": "accepted"}


@router.post("/webhooks/twilio")
async def twilio_inbound(
    request: Request,
    desk: Annotated[CounterpartyDesk, Depends(get_counterparty_desk)],
    auth_token: Annotated[str | None, Depends(get_twilio_auth_token)],
) -> Response:
    if not auth_token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "text messaging is not configured")
    raw = await request.body()
    if len(raw) > _MAX_INBOUND_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
    params = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
    url = desk.public_url(request.url.path)
    if not twilio_signature_valid(auth_token, url, params, request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid signature")
    sender = normalize_phone(params.get("From", "").removeprefix("whatsapp:"))
    try:
        await desk.reply_from_text(phone=sender, text=params.get("Body", "")[:2000])
    except (StaleReply, ValidationError):
        logger.info("text reply could not be applied")
    return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>", media_type="application/xml")


class InboundEmail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sender: str
    text: str


@router.post("/webhooks/email-inbound")
async def email_inbound(
    request: Request,
    desk: Annotated[CounterpartyDesk, Depends(get_counterparty_desk)],
    secret: Annotated[str | None, Depends(get_inbound_secret)],
) -> dict[str, str]:
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "inbound email is not configured")
    if not hmac.compare_digest(request.headers.get("X-Inbound-Secret", ""), secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid secret")
    raw = await request.body()
    if len(raw) > _MAX_INBOUND_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "too large")
    try:
        mail = InboundEmail.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expected {sender, text}") from exc
    address = re.search(r"[^\s<>\"]+@[^\s<>\"]+", mail.sender)
    if address is None:
        return {"status": "ignored"}
    # Only the reply's own first lines count: quoted history (which holds our link) is never a command.
    text = mail.text.split("\n>", 1)[0][:2000]
    try:
        handled = await desk.reply_from_text(email=address.group(0).lower(), text=text)
    except (StaleReply, ValidationError):
        return {"status": "ignored"}
    return {"status": "applied" if handled else "ignored"}


class ConfirmDeliveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool


@router.post("/runs/{run_id}/confirm-delivery")
async def confirm_delivery(
    run_id: str,
    body: ConfirmDeliveryBody,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
    runs: Annotated[RunController, Depends(get_runs)],
    processor: Annotated[EventProcessor, Depends(get_monitor_processor)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    dispatch: Annotated[MonitorDispatch | None, Depends(get_monitor_dispatch)],
) -> dict[str, str]:
    """The user confirms (or rejects) the work of a counterparty they named. Only the run's owner can, and only for
    the order in flight; the event goes through the same validation, dedupe and audit as any Monitor event."""
    if await service.get(principal.user_id, run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    state = await runs.state(run_id)
    order_id, domain = state.get("order_id"), state.get("domain")
    if state.get("counterparty") is None or not order_id or not domain or not state.get("payment_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, "there is no delivery to confirm for this run")
    monitor = get_plugin(domain).monitor
    if body.accepted:
        event_type = min(monitor.completed_event_types)
    else:
        rule = next((r for r in monitor.rules if r.problem is Problem.WORK_REJECTED), None)
        if rule is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "this run cannot reject delivery")
        event_type = rule.event_type
    event = MonitorEvent(
        event_id=f"{order_id}:{event_type}:user", run_id=run_id, source="user", domain=domain, event_type=event_type,
        entity_id=order_id, occurred_at=datetime.now(UTC), payload={},
    )
    async with session_scope(factory) as session:
        result = await processor.process(session, event)
    if result.outcome is EventOutcome.APPLIED and dispatch is not None:
        await dispatch(result)
    return {"status": result.outcome.value}

