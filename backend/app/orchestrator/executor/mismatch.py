"""Pre-execution recheck: compare approved terms with what the connector reports now.

Deterministic and domain-independent. Core rules only; a plug-in may tolerate named fields via
``MismatchPolicy.tolerated_fields`` but can never relax the price, payee, subject, deadline or
permission checks. Anything not provably inside the approval is blocked.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.approval_terms import ApprovalTerms
from app.schemas.recheck import (
    ChangeKind,
    MismatchAction,
    MismatchReport,
    ObservedTerms,
    TermChange,
)

_A = MismatchAction
_K = ChangeKind


class MismatchPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    # A total this far below the one the user saw is treated as suspicious (different item?), not a bargain.
    max_drop_ratio: float = Field(default=0.5, ge=0, le=1)
    # Dotted names ("subject_attributes.x", "constraints.x", "domain_data.x") whose drift is harmless.
    tolerated_fields: frozenset[str] = frozenset()


def _change(field: str, kind: ChangeKind, action: MismatchAction, approved: object = None, observed: object = None) -> TermChange:
    return TermChange(
        field=field,
        kind=kind,
        action=action,
        approved=None if approved is None else str(approved),
        observed=None if observed is None else str(observed),
    )


def _diff_map(
    prefix: str,
    approved: Mapping[str, object],
    observed: Mapping[str, object],
    policy: MismatchPolicy,
    *,
    ignore_extra: bool = False,
) -> list[TermChange]:
    """Compare what the user approved with what is observed, key by key.

    A key the connector did not report is *unknown* (INSUFFICIENT_DATA), not a change. A different
    value, or a key that was never approved (unless ``ignore_extra``), needs re-approval.
    """
    changes = []
    for key in sorted(set(approved) | set(observed)):
        name = f"{prefix}.{key}"
        if name in policy.tolerated_fields:
            continue
        if key not in observed:
            changes.append(_change(name, _K.FIELD_UNKNOWN, _A.INSUFFICIENT_DATA, approved[key]))
        elif key not in approved:
            if not ignore_extra:
                changes.append(_change(name, _K.FIELD_CHANGED, _A.REAPPROVE, None, observed[key]))
        elif approved[key] != observed[key]:
            changes.append(_change(name, _K.FIELD_CHANGED, _A.REAPPROVE, approved[key], observed[key]))
    return changes


def _price_changes(approved: ApprovalTerms, observed: ObservedTerms, policy: MismatchPolicy) -> list[TermChange]:
    if observed.currency.upper() != approved.currency:
        return [_change("currency", _K.CURRENCY_CHANGED, _A.REAPPROVE, approved.currency, observed.currency)]
    total = observed.total_minor
    if total is None:
        return [_change("total", _K.PRICE_UNKNOWN, _A.INSUFFICIENT_DATA)]
    if total > approved.max_total_minor:
        return [_change("total", _K.PRICE_INCREASE, _A.REAPPROVE, approved.max_total_minor, total)]
    expected = approved.expected_total_minor
    if expected and total < expected * (1 - policy.max_drop_ratio):
        return [_change("total", _K.SUSPICIOUS_DROP, _A.REAPPROVE, expected, total)]
    if expected is not None and total < expected:
        return [_change("total", _K.PRICE_DECREASE, _A.PROCEED, expected, total)]  # notify only
    return []


def _deadline_changes(approved: ApprovalTerms, observed: ObservedTerms) -> list[TermChange]:
    deadline = approved.latest_acceptable_at
    if deadline is None:
        return []
    if observed.available_by is None:
        return [_change("available_by", _K.DEADLINE_UNKNOWN, _A.INSUFFICIENT_DATA)]
    if observed.available_by > deadline:
        return [_change("available_by", _K.DEADLINE_MISSED, _A.DEADLINE_MISSED, deadline.isoformat(), observed.available_by.isoformat())]
    return []


def classify_mismatch(
    approved: ApprovalTerms, observed: ObservedTerms, policy: MismatchPolicy | None = None
) -> MismatchReport:
    policy = policy or MismatchPolicy()
    changes: list[TermChange] = []

    if observed.available is False:
        changes.append(_change("available", _K.UNAVAILABLE, _A.UNAVAILABLE, True, False))
    elif observed.available is None:
        changes.append(_change("available", _K.AVAILABILITY_UNKNOWN, _A.INSUFFICIENT_DATA))

    changes += _price_changes(approved, observed, policy)
    changes += _deadline_changes(approved, observed)

    for name in ("subject_id", "quantity"):
        if getattr(observed, name) != getattr(approved, name):
            changes.append(_change(name, _K.FIELD_CHANGED, _A.REAPPROVE, getattr(approved, name), getattr(observed, name)))
    if extra := observed.permissions - approved.permissions:
        changes.append(_change("permissions", _K.SCOPE_INCREASED, _A.REAPPROVE, observed=",".join(sorted(extra))))

    # Descriptive attributes may carry extra connector fields; everything else must match exactly.
    changes += _diff_map("subject_attributes", approved.subject_attributes, observed.subject_attributes, policy, ignore_extra=True)
    for name in ("parties", "constraints", "domain_data"):
        changes += _diff_map(name, getattr(approved, name), getattr(observed, name), policy)

    action = max((c.action for c in changes), default=_A.PROCEED)
    return MismatchReport(action=action, changes=tuple(changes))
