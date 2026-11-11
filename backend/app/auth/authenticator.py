"""The request authenticator installed on ``app.state.authenticator`` (consumed by ``get_principal``).

Credentials: ``Authorization: Bearer <token>`` or the HTTP-only session cookie. Every failure is the same generic
401, so a caller learns nothing about why. No configured method means nothing authenticates (fail closed)."""

from datetime import timedelta

from fastapi import FastAPI, HTTPException, Request, status

from app.api.deps import Authenticator, Principal
from app.auth.external import ExternalJwtVerifier, InvalidToken
from app.auth.sessions import COOKIE_NAME, InvalidSession, SessionCodec
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _unauthorized() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", headers={"WWW-Authenticate": "Bearer"})


def bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def build_authenticator(sessions: SessionCodec | None, external: ExternalJwtVerifier | None) -> Authenticator:
    async def authenticate(request: Request) -> Principal:
        token = bearer_token(request)
        if sessions is not None:
            for candidate in (token, request.cookies.get(COOKIE_NAME)):
                if candidate:
                    try:
                        return sessions.verify(candidate)
                    except InvalidSession:
                        pass
        if external is not None and token:
            try:
                return await external.verify(token)
            except InvalidToken:
                pass
        raise _unauthorized()

    return authenticate


def install_auth(app: FastAPI, settings: Settings) -> None:
    """Wire the configured sign-in methods. Misconfiguration disables that method (and says so); it never opens one."""
    sessions: SessionCodec | None = None
    if settings.sandbox_auth_active:
        key = settings.session_signing_key.get_secret_value() if settings.session_signing_key else ""
        try:
            sessions = SessionCodec(key, timedelta(seconds=settings.session_ttl_seconds))
        except ValueError as exc:
            logger.error("sandbox sign-in disabled: %s", exc)
    elif settings.auth_sandbox_enabled:
        logger.warning("AUTH_SANDBOX_ENABLED is ignored outside ENVIRONMENT=development")
    external = (
        ExternalJwtVerifier(
            issuer=settings.auth_jwt_issuer or "", audience=settings.auth_jwt_audience or "", jwks_url=settings.auth_jwks_url or ""
        )
        if settings.jwt_auth_active
        else None
    )
    if sessions is None and external is None:
        logger.error("no authentication method is configured: every protected route will answer 401")
    app.state.session_codec = sessions
    app.state.authenticator = build_authenticator(sessions, external) if (sessions or external) else None
