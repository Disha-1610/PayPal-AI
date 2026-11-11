"""Authentication: sandbox sessions, external JWTs, the request authenticator, sign-in routes and CORS."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from pydantic import SecretStr

from app.api.deps import Principal, get_principal
from app.api.main import app as real_app
from app.api.routes import auth as auth_routes
from app.auth.authenticator import build_authenticator, install_auth
from app.auth.external import ExternalJwtVerifier, InvalidToken
from app.auth.sessions import COOKIE_NAME, InvalidSession, SessionCodec
from app.core.config import Settings

KEY = "s" * 32
NOW = datetime(2026, 11, 11, tzinfo=UTC)
ISSUER, AUDIENCE = "https://idp.test/", "paypal-ai"


def codec(ttl=timedelta(hours=1)):
    return SessionCodec(KEY, ttl)


def settings(**override) -> Settings:
    return Settings(_env_file=None, environment="development", **override)


# ---- sandbox session tokens ------------------------------------------------------------------------------

def test_session_roundtrip_carries_user_and_a_fresh_session_id():
    token, issued = codec().issue("u1")
    assert codec().verify(token) == issued and issued.user_id == "u1"
    assert codec().issue("u1")[1].session_id != issued.session_id


@pytest.mark.parametrize("mutate", ["expired", "tampered", "wrong_key", "alg_none", "missing_claims", "garbage"])
def test_bad_session_tokens_are_rejected(mutate):
    good, _ = codec().issue("u1")
    token = {
        "expired": codec(timedelta(seconds=-60)).issue("u1")[0],
        "tampered": good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"),
        "wrong_key": SessionCodec("x" * 32, timedelta(hours=1)).issue("u1")[0],
        "alg_none": jwt.encode({"sub": "u1", "sid": "s", "iss": "paypal-ai-sandbox", "exp": NOW + timedelta(days=9999)}, None, algorithm="none"),
        "missing_claims": jwt.encode({"sub": "u1"}, KEY, algorithm="HS256"),
        "garbage": "not-a-token",
    }[mutate]
    with pytest.raises(InvalidSession):
        codec().verify(token)


def test_short_signing_key_is_refused():
    with pytest.raises(ValueError, match="at least 32"):
        SessionCodec("short", timedelta(hours=1))


# ---- external JWTs ---------------------------------------------------------------------------------------

PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def external(monkeypatch) -> ExternalJwtVerifier:
    verifier = ExternalJwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks_url="https://idp.test/jwks")
    monkeypatch.setattr(verifier._jwks, "get_signing_key_from_jwt", lambda token: SimpleNamespace(key=PRIVATE.public_key()))
    return verifier


def idp_token(key=PRIVATE, **claims):
    base = {"sub": "user-9", "iss": ISSUER, "aud": AUDIENCE, "exp": datetime.now(UTC) + timedelta(minutes=5), "sid": "sess-1"}
    return jwt.encode({**base, **claims}, key, algorithm="RS256")


async def test_valid_idp_token_yields_the_principal(monkeypatch):
    assert await external(monkeypatch).verify(idp_token()) == Principal(user_id="user-9", session_id="sess-1")


async def test_session_id_falls_back_to_jti_then_a_token_hash(monkeypatch):
    verifier = external(monkeypatch)
    assert (await verifier.verify(idp_token(sid=None, jti="j1"))).session_id == "j1"
    assert len((await verifier.verify(idp_token(sid=None))).session_id) == 16


@pytest.mark.parametrize(
    "token_args",
    [
        {"iss": "https://evil.test/"}, {"aud": "someone-else"}, {"exp": datetime.now(UTC) - timedelta(minutes=5)},
        {"sub": None}, {"key": OTHER},
    ],
)
async def test_idp_tokens_that_fail_any_check_are_rejected(monkeypatch, token_args):
    claims = dict(token_args)
    key = claims.pop("key", PRIVATE)
    token = idp_token(key, **claims)
    if claims.get("sub", "x") is None:
        token = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "exp": datetime.now(UTC) + timedelta(minutes=5)}, PRIVATE, algorithm="RS256")
    with pytest.raises(InvalidToken):
        await external(monkeypatch).verify(token)


async def test_hs256_token_signed_with_the_public_key_is_rejected(monkeypatch):
    """Algorithm-confusion attempt: only asymmetric algorithms are accepted for the IdP."""
    forged = jwt.encode({"sub": "admin", "iss": ISSUER, "aud": AUDIENCE, "exp": NOW + timedelta(days=9999)}, "x" * 40, algorithm="HS256")
    with pytest.raises(InvalidToken):
        await external(monkeypatch).verify(forged)


async def test_jwks_failure_fails_closed(monkeypatch):
    verifier = ExternalJwtVerifier(issuer=ISSUER, audience=AUDIENCE, jwks_url="https://idp.test/jwks")

    def down(token):
        raise OSError("unreachable")

    monkeypatch.setattr(verifier._jwks, "get_signing_key_from_jwt", down)
    with pytest.raises(InvalidToken):
        await verifier.verify(idp_token())


# ---- authenticator + routes ------------------------------------------------------------------------------

def make_app(sessions=None, ext=None) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)

    @app.get("/protected")
    async def protected(principal: Principal = Depends(get_principal)):
        return {"user": principal.user_id}

    app.state.session_codec = sessions
    app.state.authenticator = build_authenticator(sessions, ext) if (sessions or ext) else None
    return app


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_no_method_configured_means_everything_is_401():
    c = client(make_app())
    assert (await c.get("/protected")).status_code == 401
    assert (await c.post("/auth/sandbox-session")).status_code == 404


async def test_sandbox_sign_in_sets_an_httponly_cookie_that_authenticates_and_is_reused():
    c = client(make_app(codec()))
    first = await c.post("/auth/sandbox-session")
    cookie = first.headers["set-cookie"].lower()
    assert first.status_code == 200 and "httponly" in cookie and "samesite=lax" in cookie and "max-age=3600" in cookie
    assert (await c.get("/protected")).json() == {"user": first.json()["user_id"]}
    again = await c.post("/auth/sandbox-session")  # cookie jar is kept: same user, no new cookie
    assert again.json() == first.json() and "set-cookie" not in again.headers


async def test_bearer_session_token_works_and_a_tampered_one_does_not():
    sessions = codec()
    token, principal = sessions.issue("u7")
    c = client(make_app(sessions))
    assert (await c.get("/protected", headers={"Authorization": f"Bearer {token}"})).json() == {"user": "u7"}
    bad = await c.get("/protected", headers={"Authorization": f"Bearer {token}x"})
    assert bad.status_code == 401 and bad.json() == {"detail": "authentication required"}
    assert bad.headers["www-authenticate"] == "Bearer"


async def test_every_failure_looks_the_same_to_the_caller(monkeypatch):
    c = client(make_app(codec(), external(monkeypatch)))
    responses = [
        await c.get("/protected"),
        await c.get("/protected", headers={"Authorization": "Bearer garbage"}),
        await c.get("/protected", headers={"Authorization": "Basic abc"}),
        await c.get("/protected", headers={"Authorization": f"Bearer {idp_token(key=OTHER)}"}),
    ]
    assert {(r.status_code, r.text) for r in responses} == {(401, '{"detail":"authentication required"}')}


async def test_idp_bearer_authenticates_when_configured(monkeypatch):
    c = client(make_app(None, external(monkeypatch)))
    assert (await c.get("/protected", headers={"Authorization": f"Bearer {idp_token()}"})).json() == {"user": "user-9"}
    assert (await c.get("/auth/me", headers={"Authorization": f"Bearer {idp_token()}"})).json() == {"user_id": "user-9"}


async def test_logout_clears_the_cookie():
    c = client(make_app(codec()))
    await c.post("/auth/sandbox-session")
    response = await c.post("/auth/logout")
    assert response.status_code == 204 and "max-age=0" in response.headers["set-cookie"].lower()
    assert (await c.get("/protected")).status_code == 401


# ---- install_auth ----------------------------------------------------------------------------------------

def installed(**override):
    app = FastAPI()
    install_auth(app, settings(**override))
    return app


def test_sandbox_needs_development_and_a_long_key():
    assert installed(auth_sandbox_enabled=True, session_signing_key=SecretStr(KEY)).state.session_codec is not None
    production = Settings(_env_file=None, environment="production", auth_sandbox_enabled=True, session_signing_key=SecretStr(KEY))
    app = FastAPI()
    install_auth(app, production)
    assert app.state.session_codec is None and app.state.authenticator is None
    assert installed(auth_sandbox_enabled=True, session_signing_key=SecretStr("short")).state.authenticator is None
    assert installed(auth_sandbox_enabled=True).state.authenticator is None  # no key at all


def test_jwt_auth_needs_all_three_settings():
    assert installed(auth_jwt_issuer=ISSUER, auth_jwt_audience=AUDIENCE).state.authenticator is None
    full = installed(auth_jwt_issuer=ISSUER, auth_jwt_audience=AUDIENCE, auth_jwks_url="https://idp.test/jwks")
    assert full.state.authenticator is not None and full.state.session_codec is None


# ---- CORS ------------------------------------------------------------------------------------------------

async def test_cors_allows_only_configured_origins_with_credentials():
    c = client(real_app)
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "idempotency-key,content-type"}
    ok = await c.options("/runs", headers={"Origin": "http://localhost:3000", **preflight})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert ok.headers["access-control-allow-credentials"] == "true"
    evil = await c.options("/runs", headers={"Origin": "https://evil.test", **preflight})
    assert "access-control-allow-origin" not in evil.headers
    assert "*" not in ok.headers["access-control-allow-origin"]
