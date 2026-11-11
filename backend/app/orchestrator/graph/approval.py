"""Approval nodes. Split in two because LangGraph re-runs an interrupted node on resume: the audit event and
state write live in ``request_approval_node`` (runs once), the pause alone in ``await_approval_node``.

The approval itself is minted by the API handler (``issue_approval`` after an authenticated user action) and
arrives through the resume value. The node never trusts it blindly: it must carry the pending terms' hash.
"""

from typing import Any

from langgraph.types import interrupt
from pydantic import ValidationError

from app.audit.approvals import record_approval_requested
from app.audit.events import AuditScope
from app.schemas.authorization import Approval
from app.schemas.state import TaskState

APPROVAL_WRITABLE = frozenset({"approvals", "pending_approval", "status", "errors"})


def request_approval_node(state: TaskState, *, audit: AuditScope | None = None) -> dict[str, object]:
    pending = state.get("pending_approval")
    if pending is None:
        return {"status": "failed", "errors": ["approval requested with no pending terms"]}
    if audit:
        record_approval_requested(audit, pending)
    return {"status": "awaiting_approval"}


def await_approval_node(state: TaskState) -> dict[str, object]:
    pending = state["pending_approval"]
    assert pending is not None  # request_approval_node guarantees it
    raw: Any = interrupt({"kind": "approval", "terms_hash": pending.terms_hash, "action": pending.action.value})
    cleared: dict[str, object] = {"pending_approval": None}
    if not isinstance(raw, dict) or raw.get("decision") != "approve":
        return {**cleared, "status": "approval_rejected"}
    try:
        approvals = [Approval.model_validate(a) for a in raw.get("approvals") or []]
    except ValidationError:
        return {**cleared, "status": "approval_rejected", "errors": ["approval payload was malformed"]}
    if not approvals or any(a.terms_hash != pending.terms_hash or a.run_id != state.get("run_id") for a in approvals):
        return {**cleared, "status": "approval_rejected", "errors": ["approval does not match the pending terms"]}
    return {**cleared, "approvals": approvals, "status": "approved"}
