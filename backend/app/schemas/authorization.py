"""Task-level authorization and approval.

Three distinct concepts (never merge them):
  * Constraint    - what the user wants          (``schemas.constraints``)
  * Authorization - what the agent may do        (``TaskAuthorization``)
  * Approval      - what the user confirmed      (``Approval``)

Example: constraint "shoes < 5000", authorization "spend up to 5000",
approval "buy this specific pair for 4500".
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.approval_terms import ApprovalTerms


class Action(StrEnum):
    SEARCH = "search"
    NEGOTIATE = "negotiate"
    CREATE_ORDER = "create_order"
    AUTHORIZE_PAYMENT = "authorize_payment"
    CAPTURE_PAYMENT = "capture_payment"
    VOID_PAYMENT = "void_payment"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskAuthorization(BaseModel):
    """Immutable grant of authority for a single task/run."""

    model_config = ConfigDict(frozen=True)

    authorization_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    max_spend: Decimal = Field(ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    permitted_platforms: frozenset[str] = frozenset()
    permitted_actions: frozenset[Action] = frozenset()
    # Money is held at order creation / authorization, so those need approval as much as capture does.
    approval_required_actions: frozenset[Action] = frozenset(
        {Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT}
    )
    expires_at: datetime | None = None
    granted_at: datetime = Field(default_factory=_utcnow)


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    requires_approval: bool = False
    reason: str


class Approval(BaseModel):
    """User confirmation of one specific action on one specific candidate and amount."""

    model_config = ConfigDict(frozen=True)

    approval_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    authorization_id: str
    action: Action
    candidate_id: str
    platform: str
    amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    approved_by: str
    approved_at: datetime = Field(default_factory=_utcnow)
    # Signed-approval fields (issued by ``orchestrator.guard.approval_guard``). All optional so a
    # plain approval still works; ``max_amount`` turns the exact-amount match into a ceiling.
    max_amount: Decimal | None = Field(default=None, ge=0)
    terms: ApprovalTerms | None = None
    terms_hash: str | None = None
    nonce: str | None = None
    session_id: str | None = None
    key_id: str | None = None
    expires_at: datetime | None = None
    signature: str | None = None

    def covers(self, amount: Decimal) -> bool:
        """Exact amount, or any amount up to ``max_amount`` when the user approved a ceiling."""
        return amount <= self.max_amount if self.max_amount is not None else amount == self.amount


# Decides whether an approval is genuine (signature, terms, owner). Supplied by trusted code
# (``approval_guard.signed_approval_verifier``); this module cannot verify HMACs itself.
ApprovalVerifier = Callable[["Approval"], bool]


def evaluate_authorization(
    auth: TaskAuthorization,
    *,
    action: Action,
    platform: str,
    amount: Decimal,
    currency: str,
    approvals: list[Approval] | None = None,
    approval_verifier: ApprovalVerifier | None = None,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Deterministically decide whether ``action`` is permitted under ``auth``.

    Denies by default. An approval must match authorization, run, action, platform and currency,
    cover the amount (exactly, or up to its ceiling), be unexpired and pass ``approval_verifier``.
    Without a verifier no approval is trusted (fail closed): a hand-built ``Approval`` never unlocks
    an approval-required action.
    """
    now = now or _utcnow()
    if auth.expires_at is not None and now >= auth.expires_at:
        return AuthorizationDecision(allowed=False, reason="authorization expired")
    if action not in auth.permitted_actions:
        return AuthorizationDecision(allowed=False, reason=f"action {action.value} not permitted")
    if platform not in auth.permitted_platforms:
        return AuthorizationDecision(allowed=False, reason=f"platform {platform} not permitted")
    if currency != auth.currency:
        return AuthorizationDecision(allowed=False, reason="currency mismatch")
    if amount > auth.max_spend:
        return AuthorizationDecision(allowed=False, reason="amount exceeds max_spend")

    if action in auth.approval_required_actions:
        if approval_verifier is None:
            return AuthorizationDecision(
                allowed=False, requires_approval=True, reason="approval verifier not configured"
            )
        approved = any(
            a.authorization_id == auth.authorization_id
            and a.run_id == auth.run_id
            and a.action == action
            and a.platform == platform
            and a.covers(amount)
            and (a.expires_at is None or now < a.expires_at)
            and a.currency == currency
            and approval_verifier(a)
            for a in approvals or []
        )
        if not approved:
            return AuthorizationDecision(allowed=False, requires_approval=True, reason="user approval required")
    return AuthorizationDecision(allowed=True, reason="permitted")
