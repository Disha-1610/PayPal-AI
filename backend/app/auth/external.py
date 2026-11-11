"""Verification of access tokens issued by an external identity provider (OIDC-style JWTs signed with its JWKS).

Pinned asymmetric algorithms only (never HS256: a public key must not double as an HMAC secret); issuer, audience,
expiry and subject are all required."""

import asyncio
import hashlib

import jwt

from app.api.deps import Principal

_ALGORITHMS = ["RS256", "ES256"]
_LEEWAY_SECONDS = 30


class InvalidToken(Exception):
    """The token failed verification. The reason is deliberately not surfaced to callers."""


class ExternalJwtVerifier:
    def __init__(self, *, issuer: str, audience: str, jwks_url: str) -> None:
        self._issuer, self._audience = issuer, audience
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    async def verify(self, token: str) -> Principal:
        try:
            key = await asyncio.to_thread(self._jwks.get_signing_key_from_jwt, token)  # blocking fetch, cached
            claims = jwt.decode(
                token, key.key, algorithms=_ALGORITHMS, issuer=self._issuer, audience=self._audience, leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (jwt.PyJWTError, OSError) as exc:  # includes JWKS fetch failures: fail closed
            raise InvalidToken("invalid token") from exc
        session_id = claims.get("sid") or claims.get("jti") or hashlib.sha256(token.encode()).hexdigest()[:16]
        return Principal(user_id=str(claims["sub"]), session_id=str(session_id))
