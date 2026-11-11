"""FastAPI dependencies. Shared services live on ``app.state`` (set in the lifespan) so tests can swap them."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditWriter
from app.db.session import SessionFactory
from app.monitor.events.processor import EventProcessor
from app.monitor.webhooks.paypal import WebhookVerifier
from app.orchestrator.graph.runtime import RunController
from app.schemas.monitor import MonitorResult


@dataclass(frozen=True)
class Principal:
    """An authenticated user acting in one session. Approvals are bound to both."""

    user_id: str
    session_id: str


# Resolves the caller from the request, or raises ``HTTPException(401)``. Supplied by whatever authentication
# the deployment uses; until one is installed every approval request is refused (fail closed).
Authenticator = Callable[[Request], Awaitable[Principal]]
# Hands an applied monitor result to the worker (queue publish, or an in-process call).
MonitorDispatch = Callable[[MonitorResult], Awaitable[None]]


async def get_principal(request: Request) -> Principal:
    authenticator: Authenticator | None = getattr(request.app.state, "authenticator", None)
    if authenticator is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication is not configured")
    return await authenticator(request)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return getattr(request.app.state, "session_factory", SessionFactory)


def get_runs(request: Request) -> RunController:
    runs: RunController | None = getattr(request.app.state, "runs", None)
    if runs is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "run controller is not available")
    return runs


def get_webhook_verifier(request: Request) -> WebhookVerifier:
    verifier: WebhookVerifier | None = getattr(request.app.state, "paypal", None)
    if verifier is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "PayPal is not configured")
    return verifier


def get_monitor_dispatch(request: Request) -> MonitorDispatch | None:
    return getattr(request.app.state, "monitor_dispatch", None)


def get_monitor_processor(request: Request) -> EventProcessor:
    return EventProcessor(get_runs(request).load_task)


def get_audit_writer(request: Request) -> AuditWriter | None:
    return getattr(request.app.state, "audit_writer", None)
