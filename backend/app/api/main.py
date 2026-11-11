from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import approvals, runs, webhooks
from app.audit.service import AuditWriter
from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.payments.paypal.client import PayPalClient

setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the long-lived services. ``runs`` (RunController), ``authenticator`` and ``monitor_dispatch`` are
    installed on ``app.state`` by the process that hosts the graph; absent ones make their routes fail closed."""
    writer = AuditWriter()
    writer.start()
    app.state.audit_writer = writer
    paypal = PayPalClient(settings) if settings.paypal_client_id else None
    app.state.paypal = paypal
    try:
        yield
    finally:
        if paypal is not None:
            await paypal.aclose()
        await writer.stop()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.include_router(runs.router)
app.include_router(approvals.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}
