"""User approval endpoint. The only place an ``Approval`` is minted.

The client sends a decision and the hash of the terms it was shown; the terms themselves come from the
server-side pending approval, so a client can neither approve other terms nor widen the ceiling. The
audit row is committed before the run resumes, so the run can never act on an unrecorded approval.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import Principal, get_principal, get_run_service, get_runs, get_session_factory
from app.audit.approvals import record_approval_received
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.orchestrator.graph.runtime import RunController, UnknownRun
from app.runs.service import NotAwaiting, RunBusy, RunService
from app.orchestrator.guard.approval_guard import ApprovalError, approval_keys, approval_ttl, issue_approval

router = APIRouter(tags=["approvals"])
logger = get_logger(__name__)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    terms_hash: str


class ApprovalResponse(BaseModel):
    run_id: str
    decision: Literal["approve", "reject"]
    status: str | None = None


@router.post("/runs/{run_id}/approval", response_model=ApprovalResponse)
async def submit_approval(
    run_id: str,
    body: ApprovalDecision,
    principal: Annotated[Principal, Depends(get_principal)],
    runs: Annotated[RunController, Depends(get_runs)],
    service: Annotated[RunService, Depends(get_run_service)],
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> ApprovalResponse:
    # Ownership first: another user's run is indistinguishable from a missing one (same 404 as the run routes).
    if await service.get(principal.user_id, run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    try:
        state = await runs.state(run_id)
    except UnknownRun as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from exc
    pending = state.get("pending_approval")
    if pending is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no approval is pending for this run")
    if body.terms_hash != pending.terms_hash:  # the user saw terms that are no longer the pending ones
        raise HTTPException(status.HTTP_409_CONFLICT, "terms changed; review the current terms")

    try:  # before anything is minted or audited: a duplicate submit must leave no trace
        await service.ensure_awaiting_approval(run_id)
    except (RunBusy, NotAwaiting) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "the run is not waiting for this approval") from exc

    approve = body.decision == "approve"
    approvals = []
    if approve:
        try:
            keys = approval_keys(settings)
        except ApprovalError as exc:
            logger.error("approval signing key is not configured")
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "approvals are unavailable") from exc
        authorization = state["authorization"]
        # One user decision on these exact terms covers every money step the task requires approval for.
        approvals = [
            issue_approval(
                terms=pending.terms, run_id=run_id, authorization_id=authorization.authorization_id, action=action,
                candidate_id=pending.candidate_id, platform=pending.platform, approved_by=principal.user_id,
                session_id=principal.session_id, key=keys[settings.approval_key_id], key_id=settings.approval_key_id,
                ttl=approval_ttl(settings),
            )
            for action in sorted(authorization.approval_required_actions, key=lambda a: a.value)
        ]
    async with session_scope(factory) as session:
        await record_approval_received(
            session, run_id=run_id, pending=pending, user_id=principal.user_id, approved=approve,
            approval_ids=[a.approval_id for a in approvals],
        )
    try:
        resumed = await service.resume_approval(
            run_id, {"decision": body.decision, "approvals": [a.model_dump(mode="json") for a in approvals]}
        )
    except (RunBusy, NotAwaiting) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "the run is not waiting for this approval") from exc
    return ApprovalResponse(run_id=run_id, decision=body.decision, status=resumed.get("status"))
