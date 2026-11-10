"""Deterministic routing after the Verifier. A pure function of the verdict and the domain.

Shopping: PASS -> capture; FAIL -> void, then discovery; INSUFFICIENT_EVIDENCE -> wait for the Monitor or
the deadline. Contractor: INSUFFICIENT_EVIDENCE (including generator/critic disagreement) -> a human.
"""

from app.audit.events import AuditScope, Phase
from app.schemas.verification import VerificationVerdict
from app.schemas.verifier import VerifierRoute


def route_verification(
    verdict: VerificationVerdict, *, escalate_unresolved: bool, audit: AuditScope | None = None
) -> VerifierRoute:
    """``escalate_unresolved`` is True for contractor/A2A (a human resolves it), False for shopping (wait)."""
    match verdict:
        case VerificationVerdict.PASS:
            route = VerifierRoute.CAPTURE
        case VerificationVerdict.FAIL:
            route = VerifierRoute.VOID if escalate_unresolved else VerifierRoute.VOID_THEN_DISCOVERY
        case VerificationVerdict.INSUFFICIENT_EVIDENCE:
            route = VerifierRoute.ESCALATE_HUMAN if escalate_unresolved else VerifierRoute.WAIT
    if audit:
        audit.emit(
            Phase.EXECUTION, "VERIFICATION_ROUTED", node="router", decision=route.value,
            metadata={"verdict": verdict.value, "escalate_unresolved": escalate_unresolved},
        )
    return route
