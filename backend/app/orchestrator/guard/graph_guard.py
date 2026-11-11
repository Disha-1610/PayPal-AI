"""Guard nodes: the deterministic checkpoint on every path into the Evaluator, the Executor and Payment.

Each function is a pure check over persisted state that returns a partial state update whose ``status`` is
``GUARD_PASSED`` or ``GUARD_BLOCKED``; the graph routes on that status through a lookup table, and an unknown
status fails closed. Nothing here calls an LLM, and none of it replaces the Payment Service's own permit checks.
"""

from datetime import datetime
from decimal import Decimal

from app.audit.events import AuditScope, Phase
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, ApprovalVerifier, evaluate_authorization
from app.schemas.state import TaskState

GUARD_PASSED = "guard_passed"
GUARD_BLOCKED = "guard_blocked"


def _blocked(stage: str, reason: str, audit: AuditScope | None) -> dict[str, object]:
    if audit:
        audit.emit(Phase.EXECUTION, "GUARD_BLOCKED", node="guard", decision=stage, reason=reason)
    return {"status": GUARD_BLOCKED, "errors": [f"guard blocked {stage}: {reason}"]}


def _passed(stage: str, audit: AuditScope | None, **metadata: object) -> dict[str, object]:
    if audit:
        audit.emit(Phase.EXECUTION, "GUARD_PASSED", node="guard", decision=stage, metadata=dict(metadata))
    return {"status": GUARD_PASSED}


def guard_discovery_data(state: TaskState, *, now: datetime, audit: AuditScope | None = None) -> dict[str, object]:
    """Into the Evaluator: only candidates from platforms this task may use survive."""
    auth = state["authorization"]
    if auth.expires_at is not None and now >= auth.expires_at:
        return _blocked("evaluate", "authorization expired", audit)
    candidates = state.get("candidates", [])
    kept = [c for c in candidates if c.source in auth.permitted_platforms]
    update = _passed("evaluate", audit, dropped=len(candidates) - len(kept))
    return {**update, "candidates": kept}


def guard_execution(state: TaskState, *, now: datetime, audit: AuditScope | None = None) -> dict[str, object]:
    """Into the Executor: the chosen candidate must be one the task may order, within ``max_spend``.

    Approval is not required yet (the user is asked after the recheck), so "approval required" counts as a pass.
    """
    chosen = next((c for c in state.get("candidates", []) if c.candidate_id == state.get("selected_candidate_id")), None)
    if chosen is None:
        return _blocked("execute", "selected candidate is not in the candidate pool", audit)
    if chosen.total_cost is None:
        return _blocked("execute", "selected candidate has no known price", audit)
    auth = state["authorization"]
    decision = evaluate_authorization(
        auth, action=Action.CREATE_ORDER, platform=chosen.source, amount=chosen.total_cost,
        currency=chosen.currency or auth.currency, now=now,
    )
    if decision.allowed or decision.requires_approval:
        return _passed("execute", audit, candidate_id=chosen.candidate_id)
    return _blocked("execute", decision.reason, audit)


def guard_negotiation(state: TaskState, *, now: datetime, audit: AuditScope | None = None) -> dict[str, object]:
    """Into the Negotiator: the task must hold a user approval to negotiate around, and the NEGOTIATE grant for its platform."""
    approvals = state.get("approvals", [])
    if not approvals or approvals[-1].terms is None:
        return _blocked("negotiate", "no approved terms to negotiate around", audit)
    approved = approvals[-1]
    decision = evaluate_authorization(
        state["authorization"], action=Action.NEGOTIATE, platform=approved.platform, amount=Decimal(0), currency=approved.currency, now=now
    )
    if decision.allowed:
        return _passed("negotiate", audit, platform=approved.platform)
    return _blocked("negotiate", decision.reason, audit)


def guard_payment(
    state: TaskState, *, terms: ApprovalTerms, verifier: ApprovalVerifier, now: datetime, audit: AuditScope | None = None
) -> dict[str, object]:
    """Into Payment: the latest approval must be genuine and be for exactly these ``(item, price, seller, delivery)`` terms."""
    approvals = state.get("approvals", [])
    if not approvals:
        return _blocked("payment", "no approval on record", audit)
    approved = approvals[-1]
    if approved.terms_hash != terms.terms_hash():
        return _blocked("payment", "approval hash does not match the current terms", audit)
    decision = evaluate_authorization(
        state["authorization"], action=Action.CREATE_ORDER, platform=approved.platform, amount=approved.amount,
        currency=approved.currency, approvals=list(approvals), approval_verifier=verifier, now=now,
    )
    if decision.allowed:
        return _passed("payment", audit, terms_hash=approved.terms_hash)
    return _blocked("payment", decision.reason, audit)
