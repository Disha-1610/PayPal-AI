"""PayPal webhook intake: verify the signature, map to a ``MonitorEvent``, hand to the engine.

* Unsigned/invalid webhooks are rejected before anything is stored.
* A webhook for a payment we cannot find yet (it can beat our ledger commit) raises ``WebhookNotReady``
  *without* recording it, so the API answers 5xx and PayPal retries; recording it would dedupe the retry away.
* The ledger is not changed here. Webhooks tell the Monitor what happened; payment state is reconciled by
  the payment service against PayPal's API.
"""

from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.payment import Payment, PaymentEvent
from app.monitor.events.deadline import InvalidEvent, parse_instant
from app.monitor.events.processor import EventProcessor, TaskLoader
from app.schemas.monitor import MonitorEvent, MonitorResult

SOURCE = "paypal"
_EVENT_TYPES = {
    "PAYMENT.AUTHORIZATION.VOIDED": "PAYMENT_AUTHORIZATION_VOIDED",
    "PAYMENT.CAPTURE.COMPLETED": "PAYMENT_CAPTURED",
    "PAYMENT.CAPTURE.REFUNDED": "PAYMENT_REFUNDED",
    "PAYMENT.CAPTURE.DENIED": "PAYMENT_CAPTURE_DENIED",
}
# Ledger events whose ``provider_ref`` is the PayPal id a webhook refers to.
_LINKED_EVENTS = ("authorized", "reauthorized", "captured")


class WebhookVerifier(Protocol):
    async def verify_webhook(self, *, headers: dict[str, str], event: dict[str, Any]) -> bool: ...


class WebhookRejected(Exception):
    """The signature is missing or invalid. Nothing was stored."""


class WebhookNotReady(Exception):
    """No ledger payment matches yet. Nothing was stored; ask PayPal to retry."""


async def handle_paypal_webhook(
    session: AsyncSession,
    *,
    verifier: WebhookVerifier,
    processor: EventProcessor,
    load_task: TaskLoader,
    headers: Mapping[str, str],
    body: dict[str, Any],
) -> MonitorResult | None:
    """Return the processing result, or ``None`` for event types the Monitor does not track."""
    if not await verifier.verify_webhook(headers=dict(headers), event=body):  # ProviderError propagates: 5xx, retried
        raise WebhookRejected("webhook signature verification failed")
    event_type = _EVENT_TYPES.get(str(body.get("event_type")))
    if event_type is None:
        return None
    resource = body.get("resource")
    if not isinstance(resource, dict):
        raise InvalidEvent("webhook has no resource")
    reference = _linked_id(event_type, resource)
    payment = await _find_payment(session, reference)
    try:
        view = await load_task(payment.run_id)
    except LookupError as exc:
        raise WebhookNotReady("task not found for payment") from exc
    event = MonitorEvent(
        event_id=str(body.get("id", "")), run_id=payment.run_id, source=SOURCE, domain=view.domain,
        event_type=event_type, entity_id=f"payment:{payment.id}", occurred_at=parse_instant(body.get("create_time")),
        payload={"resource_id": reference},  # not the whole body: it can carry payer details
    )
    return await processor.process(session, event)


def _linked_id(event_type: str, resource: dict[str, Any]) -> str:
    if event_type == "PAYMENT_REFUNDED":  # a refund's own id is new to us; its "up" link names the capture
        for link in resource.get("links") or []:
            if isinstance(link, dict) and link.get("rel") == "up" and "/captures/" in str(link.get("href", "")):
                return str(link["href"]).rsplit("/captures/", 1)[1].split("/")[0]
        raise InvalidEvent("refund webhook does not name its capture")
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise InvalidEvent("webhook resource has no id")
    return resource_id


async def _find_payment(session: AsyncSession, reference: str) -> Payment:
    stmt = select(PaymentEvent.payment_id).where(
        PaymentEvent.provider_ref == reference, PaymentEvent.event_type.in_(_LINKED_EVENTS)
    )
    payment_id: UUID | None = (await session.execute(stmt)).scalars().first()
    payment = await session.get(Payment, payment_id) if payment_id else None
    if payment is None:
        raise WebhookNotReady("no payment matches this webhook yet")
    return payment
