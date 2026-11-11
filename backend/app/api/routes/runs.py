"""Run routes: create (idempotent), list (cursor-paginated, newest first) and fetch. Every query is scoped to the
authenticated user; a run that is not yours is a 404, the same as one that does not exist."""

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.deps import Principal, get_principal, get_run_service
from app.runs.service import (
    CannotCancel,
    InvalidCursor,
    NewRun,
    NotAwaiting,
    RunBusy,
    RunService,
    UnsupportedDomain,
)
from app.runs.view import MessageView, RunPageView, RunView

router = APIRouter(tags=["runs"])
_KEEP_ALIVE = ": keep-alive\n\n"  # an SSE comment; keeps proxies from closing an idle stream


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=3, max_length=2000)
    max_spend: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: Literal["USD", "INR"]
    deliver_by: datetime | None = None
    domain: Literal["shopping", "contractor", "a2a"] = "shopping"

    @model_validator(mode="after")
    def _deadline_for_engagements(self) -> "CreateRunBody":
        if self.domain != "shopping" and self.deliver_by is None:
            raise ValueError("deliver_by is required for contractor and a2a runs: it is the hard completion deadline")
        return self


class CreateRunResponse(BaseModel):
    run_id: str


@router.post("/runs", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRunBody,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
    idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
) -> CreateRunResponse:
    try:
        record = await service.create(
            principal.user_id, idempotency_key,
            NewRun(
                goal=body.goal, max_spend=body.max_spend, currency=body.currency, deliver_by=body.deliver_by, domain=body.domain
            ),
        )
    except UnsupportedDomain as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return CreateRunResponse(run_id=record.run_id)


@router.get("/runs", response_model=RunPageView, response_model_exclude_none=True)
async def list_runs(
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> RunPageView:
    try:
        items, next_cursor = await service.list_views(principal.user_id, limit=limit, cursor=cursor)
    except InvalidCursor as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc
    return RunPageView(items=items, next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=RunView, response_model_exclude_none=True)
async def get_run(
    run_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunView:
    view = await service.view(principal.user_id, run_id)
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return view


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.post(
    "/runs/{run_id}/messages", response_model=MessageView, response_model_exclude_none=True, status_code=status.HTTP_201_CREATED
)
async def send_message(
    run_id: str,
    body: MessageBody,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> MessageView:
    """Answer the question the run is waiting on. The run resumes in the background; watch the event stream."""
    try:
        message = await service.post_message(principal.user_id, run_id, body.text.strip(), body.client_id)
    except NotAwaiting as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if message is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return message


@router.post("/runs/{run_id}/payer-return", status_code=status.HTTP_202_ACCEPTED)
async def payer_return(
    run_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> Response:
    """The payer is back from PayPal. Carries no PayPal data: the backend verifies the order itself."""
    try:
        found = await service.payer_return(principal.user_id, run_id)
    except NotAwaiting as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(
    run_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> Response:
    """Cancel a paused run. Committed funds are released first; 409 if the run is busy, finished, or still holds money."""
    try:
        found = await service.cancel(principal.user_id, run_id)
    except RunBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "the run is busy; try again in a moment") from exc
    except CannotCancel as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> StreamingResponse:
    """Server-sent events: ``run.snapshot`` frames whenever the run changes."""
    if await service.get(principal.user_id, run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    async def frames() -> AsyncIterator[str]:
        async for frame in service.events(principal.user_id, run_id):
            yield _KEEP_ALIVE if frame is None else frame

    return StreamingResponse(
        frames(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
