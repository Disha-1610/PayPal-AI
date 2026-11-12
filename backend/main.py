"""Vercel entrypoint. Requests reach the backend under ``/api/*``; the API routes are unprefixed,
so strip ``/api`` before dispatching. Local dev keeps using ``app.api.main:app`` directly."""

from starlette.types import Receive, Scope, Send

from app.api.main import app as api_app

_PREFIX = "/api"


async def app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["type"] in ("http", "websocket"):
        path: str = scope["path"]
        if path == _PREFIX or path.startswith(_PREFIX + "/"):
            scope = {**scope, "path": path[len(_PREFIX):] or "/", "root_path": scope.get("root_path", "") + _PREFIX}
    await api_app(scope, receive, send)
