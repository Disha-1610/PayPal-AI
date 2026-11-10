"""Run-level execution state machine (Phase 5).

Money-level states live in the payment ledger (``PaymentStatus``); this tracks where the *execution*
of an approved agreement stands, for any domain (a contractor engagement or an A2A task may involve
no payment at all). Orchestrator code never imports the ledger models (``test_architecture``), so the
mapping is by convention: EXECUTING/AWAITING_CONFIRMATION = ledger ``created``/``authorized``,
VERIFIED = ``authorized``, SETTLED = ``captured``, VOIDED = ``voided``.
"""

from enum import StrEnum


class ExecutionState(StrEnum):
    PROPOSED = "proposed"
    RECHECKED = "rechecked"
    APPROVED = "approved"
    EXECUTING = "executing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # waiting on payer/counterparty
    VERIFIED = "verified"
    SETTLED = "settled"
    COMPLETED = "completed"
    FAILED = "failed"
    VOIDED = "voided"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_S = ExecutionState
TERMINAL = frozenset({_S.COMPLETED, _S.FAILED, _S.VOIDED, _S.CANCELLED, _S.EXPIRED})

TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    _S.PROPOSED: frozenset({_S.RECHECKED, _S.CANCELLED, _S.EXPIRED, _S.FAILED}),
    _S.RECHECKED: frozenset({_S.APPROVED, _S.PROPOSED, _S.CANCELLED, _S.EXPIRED, _S.FAILED}),
    _S.APPROVED: frozenset({_S.EXECUTING, _S.RECHECKED, _S.CANCELLED, _S.EXPIRED, _S.FAILED}),
    _S.EXECUTING: frozenset({_S.AWAITING_CONFIRMATION, _S.VOIDED, _S.FAILED}),
    _S.AWAITING_CONFIRMATION: frozenset({_S.VERIFIED, _S.VOIDED, _S.FAILED, _S.EXPIRED}),
    _S.VERIFIED: frozenset({_S.SETTLED, _S.COMPLETED, _S.VOIDED, _S.FAILED}),
    _S.SETTLED: frozenset({_S.COMPLETED}),
    **{state: frozenset() for state in TERMINAL},
}

class InvalidExecutionTransition(ValueError):
    """The requested execution state does not follow from the current one."""


def advance(current: ExecutionState, target: ExecutionState) -> ExecutionState:
    if target not in TRANSITIONS[current]:
        raise InvalidExecutionTransition(f"{current.value} -> {target.value} is not allowed")
    return target
