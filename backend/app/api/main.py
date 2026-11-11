from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.bootstrap import RuntimeConfigError, open_runtime
from app.api.routes import approvals, runs, webhooks
from app.audit.service import AuditWriter
from app.core.config import settings
from app.core.logging import get_logger, setup_logging

setup_logging(settings.log_level)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the long-lived services. ``open_runtime`` installs the graph-backed services on ``app.state``;
    ``authenticator`` is installed separately, and absent ones make their routes fail closed."""
    writer = AuditWriter()
    writer.start()
    app.state.audit_writer = writer
    try:
        try:
            async with open_runtime(app, settings):
                yield
        except RuntimeConfigError as exc:
            # Degraded start: routes that need the runtime answer 503 instead of guessing.
            logger.error("runtime not started: %s", exc)
            yield
    finally:
        await writer.stop()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.include_router(runs.router)
app.include_router(approvals.router)
app.include_router(webhooks.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "environment": settings.environment}
