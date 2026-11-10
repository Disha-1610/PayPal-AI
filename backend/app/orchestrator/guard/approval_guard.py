"""Approval signing and verification (Phase 5).

An approval is only valid if it was issued by trusted backend code after an authenticated user
action. ``issue_approval`` / ``verify_approval`` are deterministic: the LLM never sees the key and
can neither mint nor alter an approval. The HMAC proves *the backend issued this exact token*; the
authenticated approval event (``approved_by``, audit row) is the evidence of consent.

``signed_approval_verifier`` plugs this into ``evaluate_authorization``, which refuses every
approval-required action unless a verifier is supplied (a hand-built ``Approval`` is never enough).

Single-use consumption is NOT decided here: it needs an atomic DB write (unique ``approval_id``),
done by the executor. ``verify_approval`` only reports whether the approval is currently valid.
"""

import hmac
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings
from app.core.hashing import canonical_json
from app.schemas.approval_terms import ApprovalTerms, from_minor_units, to_minor_units
from app.schemas.authorization import Action, Approval, ApprovalVerifier


class ApprovalError(ValueError):
    """An approval could not be issued or the signing configuration is missing."""


class ApprovalStatus(StrEnum):
    VALID = "valid"
    UNSIGNED = "unsigned"
    BAD_SIGNATURE = "bad_signature"  # forged token, or stored terms that do not match the signed hash
    UNKNOWN_KEY = "unknown_key"
    EXPIRED = "expired"
    TERMS_CHANGED = "terms_changed"
    WRONG_SUBJECT = "wrong_subject"  # different user, session, run or action


class ApprovalVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: ApprovalStatus
    detail: str = ""

    @property
    def valid(self) -> bool:
        return self.status is ApprovalStatus.VALID


def approval_keys(settings: Settings) -> dict[str, bytes]:
    """Signing keys by id, from settings. Raises if the key is not configured."""
    if settings.approval_signing_key is None or not settings.approval_signing_key.get_secret_value():
        raise ApprovalError("APPROVAL_SIGNING_KEY is not configured")
    return {settings.approval_key_id: settings.approval_signing_key.get_secret_value().encode()}


def approval_ttl(settings: Settings) -> timedelta:
    return timedelta(seconds=settings.approval_ttl_seconds)


def _message(approval: Approval, terms_hash: str) -> bytes:
    """Everything the signature binds: the terms plus who/where/when/which action.

    Money is signed as integer minor units so Decimal scale ('5500' vs '5500.00') cannot change it.
    """
    assert approval.expires_at is not None
    ceiling = approval.max_amount if approval.max_amount is not None else approval.amount
    return canonical_json(
        {
            "approval_id": approval.approval_id,
            "run_id": approval.run_id,
            "authorization_id": approval.authorization_id,
            "action": approval.action.value,
            "candidate_id": approval.candidate_id,
            "platform": approval.platform,
            "currency": approval.currency,
            "max_amount_minor": to_minor_units(ceiling, approval.currency),
            "approved_by": approval.approved_by,
            "session_id": approval.session_id,
            "nonce": approval.nonce,
            "key_id": approval.key_id,
            "expires_at": approval.expires_at.astimezone(UTC).isoformat(),
            "terms_hash": terms_hash,
        }
    ).encode()


def _sign(key: bytes, approval: Approval, terms_hash: str) -> str:
    return hmac.new(key, _message(approval, terms_hash), sha256).hexdigest()


def issue_approval(
    *,
    terms: ApprovalTerms,
    run_id: str,
    authorization_id: str,
    action: Action,
    candidate_id: str,
    platform: str,
    approved_by: str,
    session_id: str,
    key: bytes,
    key_id: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> Approval:
    """Issue a signed approval. Call ONLY from the authenticated user-approval handler."""
    if not key:
        raise ApprovalError("approval signing key is not configured")
    now = now or datetime.now(UTC)
    max_amount = from_minor_units(terms.max_total_minor, terms.currency)
    unsigned = Approval(
        run_id=run_id,
        authorization_id=authorization_id,
        action=action,
        candidate_id=candidate_id,
        platform=platform,
        amount=max_amount,
        max_amount=max_amount,
        currency=terms.currency,
        approved_by=approved_by,
        approved_at=now,
        terms=terms,
        terms_hash=terms.terms_hash(),
        nonce=uuid4().hex,
        session_id=session_id,
        key_id=key_id,
        expires_at=now + ttl,
    )
    return unsigned.model_copy(update={"signature": _sign(key, unsigned, unsigned.terms_hash or "")})


def verify_approval(
    approval: Approval,
    *,
    current_terms: ApprovalTerms,
    keys: dict[str, bytes],
    run_id: str,
    session_id: str,
    approved_by: str,
    action: Action,
    now: datetime | None = None,
) -> ApprovalVerdict:
    """Recompute the signature over the *current* terms and check expiry and ownership."""
    now = now or datetime.now(UTC)
    if not (approval.signature and approval.terms_hash and approval.nonce and approval.expires_at and approval.key_id):
        return ApprovalVerdict(status=ApprovalStatus.UNSIGNED, detail="approval carries no signature")
    key = keys.get(approval.key_id)
    if key is None:
        return ApprovalVerdict(status=ApprovalStatus.UNKNOWN_KEY, detail=f"key {approval.key_id!r} not loaded")
    # Tampered token fails here, before anything else is trusted.
    if not hmac.compare_digest(approval.signature, _sign(key, approval, approval.terms_hash)):
        return ApprovalVerdict(status=ApprovalStatus.BAD_SIGNATURE, detail="signature mismatch")
    # The signature binds terms_hash, not the stored terms object: make sure they agree so anything
    # reading approval.terms later reads what the user actually approved.
    if approval.terms is None or approval.terms.terms_hash() != approval.terms_hash:
        return ApprovalVerdict(status=ApprovalStatus.BAD_SIGNATURE, detail="stored terms do not match the signed hash")
    if now >= approval.expires_at:
        return ApprovalVerdict(status=ApprovalStatus.EXPIRED, detail="approval expired")
    if (approval.run_id, approval.session_id, approval.approved_by, approval.action) != (
        run_id, session_id, approved_by, action,
    ):
        return ApprovalVerdict(status=ApprovalStatus.WRONG_SUBJECT, detail="approval belongs to another run/user/session/action")
    if approval.terms_hash != current_terms.terms_hash():
        return ApprovalVerdict(status=ApprovalStatus.TERMS_CHANGED, detail="terms differ from those approved")
    return ApprovalVerdict(status=ApprovalStatus.VALID)


def signed_approval_verifier(
    *,
    current_terms: ApprovalTerms,
    keys: dict[str, bytes],
    run_id: str,
    session_id: str,
    approved_by: str,
    now: datetime | None = None,
) -> ApprovalVerifier:
    """Build the verifier ``evaluate_authorization`` needs: valid signature over the *current* terms.

    Each approval is checked against its own ``action``, so one verifier serves create, authorize and capture.
    """

    def verify(approval: Approval) -> bool:
        return verify_approval(
            approval, current_terms=current_terms, keys=keys, run_id=run_id, session_id=session_id,
            approved_by=approved_by, action=approval.action, now=now,
        ).valid

    return verify
