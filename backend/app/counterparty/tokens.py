"""Signed reply links. A token names one run, the stage (quote / counter) and the round it answers, and expires;
a reply with a token for an earlier round is refused, so an old link cannot reopen a settled negotiation."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

Stage = Literal["quote", "counter"]
_ALGORITHM = "HS256"
_ISSUER = "aisling-counterparty"
MIN_KEY_LENGTH = 32


class InvalidReplyToken(Exception):
    """Missing, expired, tampered with or not ours."""


@dataclass(frozen=True)
class ReplyClaim:
    run_id: str
    stage: Stage
    round: int


@dataclass(frozen=True)
class ReplyTokens:
    key: str
    ttl: timedelta = timedelta(days=14)

    def __post_init__(self) -> None:
        if len(self.key) < MIN_KEY_LENGTH:
            raise ValueError(f"the counterparty signing key must be at least {MIN_KEY_LENGTH} characters")

    def issue(self, claim: ReplyClaim, *, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        claims = {"sub": claim.run_id, "stg": claim.stage, "rnd": claim.round, "iss": _ISSUER, "iat": now, "exp": now + self.ttl}
        return jwt.encode(claims, self.key, algorithm=_ALGORITHM)

    def verify(self, token: str) -> ReplyClaim:
        try:
            claims = jwt.decode(
                token, self.key, algorithms=[_ALGORITHM], issuer=_ISSUER, options={"require": ["exp", "sub", "stg", "rnd", "iss"]}
            )
        except jwt.PyJWTError as exc:
            raise InvalidReplyToken("invalid reply link") from exc
        stage, round_ = claims["stg"], claims["rnd"]
        if stage not in ("quote", "counter") or not isinstance(round_, int):
            raise InvalidReplyToken("invalid reply link")
        return ReplyClaim(run_id=str(claims["sub"]), stage=stage, round=round_)
