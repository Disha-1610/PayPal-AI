"""Audit events for the user-approval step: requested (by the graph) and received (by the API handler).

``APPROVAL_RECEIVED`` is a critical event written in the same transaction as the approval it records, so an
approval can never be issued without its audit row. ``APPROVAL_REQUESTED`` is emitted before the graph pauses.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.events import AuditScope, Phase, emit_critical
from app.schemas.authorization import PendingApproval

NODE_REQUEST = "approval_request"
NODE_RECEIVE = "approval_api"


def record_approval_requested(audit: AuditScope, pending: PendingApproval) -> None:
    audit.emit(
        Phase.EXECUTION, "APPROVAL_REQUESTED", node=NODE_REQUEST, resource_type="approval_terms",
        resource_id=pending.terms_hash, payload=pending.terms.canonical(), decision="requested",
        metadata={"action": pending.action.value, "candidate_id": pending.candidate_id, "platform": pending.platform},
    )


async def record_approval_received(
    session: AsyncSession, *, run_id: str, pending: PendingApproval, user_id: str, approved: bool, approval_ids: list[str]
) -> None:
    await emit_critical(
        session, run_id, Phase.EXECUTION, "APPROVAL_RECEIVED", node=NODE_RECEIVE, actor_type="user", actor_id=user_id,
        resource_type="approval_terms", resource_id=pending.terms_hash, payload=pending.terms.canonical(),
        decision="approved" if approved else "rejected",
        metadata={"action": pending.action.value, "candidate_id": pending.candidate_id, "approval_ids": approval_ids},
    )
