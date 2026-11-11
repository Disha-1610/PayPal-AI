"""Sign-in routes. ``/auth/sandbox-session`` exists only while the sandbox sign-in is active (development); with
any other configuration it is a 404, as if it were not there. ``/auth/me`` works for every sign-in method."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.api.deps import Principal, get_principal
from app.auth.sessions import COOKIE_NAME, InvalidSession, SessionCodec

router = APIRouter(tags=["auth"])


class Me(BaseModel):
    user_id: str


@router.post("/auth/sandbox-session", response_model=Me)
async def sandbox_session(request: Request, response: Response) -> Me:
    """Start (or keep) an anonymous sandbox session. Reuses a valid cookie so a reload stays the same user."""
    codec: SessionCodec | None = getattr(request.app.state, "session_codec", None)
    if codec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    existing = request.cookies.get(COOKIE_NAME)
    if existing:
        try:
            return Me(user_id=codec.verify(existing).user_id)
        except InvalidSession:
            pass
    token, principal = codec.issue(f"sandbox-{uuid4().hex}")
    response.set_cookie(
        COOKIE_NAME, token, max_age=int(codec.ttl.total_seconds()), httponly=True, samesite="lax", secure=False, path="/"
    )
    return Me(user_id=principal.user_id)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/auth/me", response_model=Me)
async def me(principal: Annotated[Principal, Depends(get_principal)]) -> Me:
    return Me(user_id=principal.user_id)
