"""PayPal webhook endpoint.

Status codes drive PayPal's retry: 401 for a bad signature (never stored), 503 when we are not ready or PayPal
is unreachable (PayPal retries, and nothing was recorded so the retry is not deduped away), 200 otherwise.
The ledger is untouched here; payment state comes from the payment service, never from a webhook or redirect.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import (
    MonitorDispatch,
    get_monitor_dispatch,
    get_monitor_processor,
    get_runs,
    get_session_factory,
    get_webhook_verifier,
)
from app.core.logging import get_logger
from app.db.session import session_scope
from app.monitor.events.deadline import InvalidEvent
from app.monitor.events.processor import EventProcessor
from app.monitor.webhooks.paypal import (
    WebhookNotReady,
    WebhookRejected,
    WebhookVerifier,
    handle_paypal_webhook,
)
from app.orchestrator.graph.runtime import RunController
from app.payments.provider import ProviderError
from app.schemas.monitor import EventOutcome

router = APIRouter(tags=["webhooks"])
logger = get_logger(__name__)


@router.post("/webhooks/paypal")
async def paypal_webhook(
    request: Request,
    verifier: Annotated[WebhookVerifier, Depends(get_webhook_verifier)],
    processor: Annotated[EventProcessor, Depends(get_monitor_processor)],
    runs: Annotated[RunController, Depends(get_runs)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    dispatch: Annotated[MonitorDispatch | None, Depends(get_monitor_dispatch)],
) -> dict[str, str]:
    try:
        body: Any = await request.json()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not an object")
    try:
        async with session_scope(factory) as session:
            result = await handle_paypal_webhook(
                session, verifier=verifier, processor=processor, load_task=runs.load_task,
                headers=request.headers, body=body,
            )
    except WebhookRejected as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook signature") from exc
    except (WebhookNotReady, ProviderError) as exc:
        logger.warning("webhook not processed; PayPal will retry", extra={"reason": type(exc).__name__})
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "not ready, retry later") from exc
    except InvalidEvent:
        return {"status": "ignored"}  # signed but malformed: retrying cannot fix it
    if result is None:
        return {"status": "ignored"}
    if result.outcome is EventOutcome.APPLIED and dispatch is not None:
        await dispatch(result)  # after commit; the event is already stored if this fails
    return {"status": result.outcome.value}
