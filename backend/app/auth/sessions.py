"""Sandbox session tokens: HS256 JWTs this backend signs itself. Used only for the development sign-in; the
algorithm is pinned on verify so a token can never be accepted under a different one."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.api.deps import Principal

COOKIE_NAME = "pp_session"
_ALGORITHM = "HS256"
_ISSUER = "paypal-ai-sandbox"
MIN_KEY_LENGTH = 32


class InvalidSession(Exception):
    """The token is missing, expired, tampered with or not ours."""


@dataclass(frozen=True)
class SessionCodec:
    key: str
    ttl: timedelta

    def __post_init__(self) -> None:
        if len(self.key) < MIN_KEY_LENGTH:
            raise ValueError(f"the session signing key must be at least {MIN_KEY_LENGTH} characters")

    def issue(self, user_id: str, *, now: datetime | None = None) -> tuple[str, Principal]:
        now = now or datetime.now(UTC)
        principal = Principal(user_id=user_id, session_id=secrets.token_hex(8))
        claims = {"sub": user_id, "sid": principal.session_id, "iss": _ISSUER, "iat": now, "exp": now + self.ttl}
        return jwt.encode(claims, self.key, algorithm=_ALGORITHM), principal

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token, self.key, algorithms=[_ALGORITHM], issuer=_ISSUER, options={"require": ["exp", "sub", "sid", "iss"]}
            )
        except jwt.PyJWTError as exc:
            raise InvalidSession("invalid session token") from exc
        return Principal(user_id=str(claims["sub"]), session_id=str(claims["sid"]))
