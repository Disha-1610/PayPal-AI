"""Run routes: create (idempotent), list (cursor-paginated, newest first) and fetch. Every query is scoped to the
authenticated user; a run that is not yours is a 404, the same as one that does not exist."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Principal, get_principal, get_run_service
from app.runs.service import InvalidCursor, NewRun, RunService
from app.runs.view import RunPageView, RunView, run_view

router = APIRouter(tags=["runs"])


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=3, max_length=2000)
    max_spend: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: Literal["USD", "INR"]
    deliver_by: datetime | None = None


class CreateRunResponse(BaseModel):
    run_id: str


@router.post("/runs", response_model=CreateRunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    body: CreateRunBody,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
    idempotency_key: Annotated[str, Header(min_length=8, max_length=128)],
) -> CreateRunResponse:
    record = await service.create(
        principal.user_id, idempotency_key,
        NewRun(goal=body.goal, max_spend=body.max_spend, currency=body.currency, deliver_by=body.deliver_by),
    )
    return CreateRunResponse(run_id=record.run_id)


@router.get("/runs", response_model=RunPageView)
async def list_runs(
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> RunPageView:
    try:
        page = await service.list(principal.user_id, limit=limit, cursor=cursor)
    except InvalidCursor as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from exc
    return RunPageView(items=[run_view(r) for r in page.items], next_cursor=page.next_cursor)


@router.get("/runs/{run_id}", response_model=RunView)
async def get_run(
    run_id: str,
    principal: Annotated[Principal, Depends(get_principal)],
    service: Annotated[RunService, Depends(get_run_service)],
) -> RunView:
    record = await service.get(principal.user_id, run_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return run_view(record)
