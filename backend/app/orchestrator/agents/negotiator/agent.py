"""Negotiator: proposes a response to a counter-offer or a failed delivery, only inside the user's envelope.

The model returns an action enum (accept / counter / stop) plus numbers; it never routes. Every candidate
offer goes through the deterministic Guard: a BLOCKED proposal is fed back for one retry, then the
fail-safe is STOP (report to the user). Rounds are capped: past the cap the agent stops, and in the last
round it may only accept or stop. An accepted offer whose terms hash changed carries
``needs_fresh_approval``; the Router sends it to the user, then Executor / Authorize. This agent returns a
result and never calls another agent.
"""

import json

from app.audit.events import AuditScope, Phase
from app.core.logging import get_logger
from app.llm.gateway import LLMGatewayError, call
from app.llm.providers import LLMProvider
from app.orchestrator.guard.negotiation_guard import check_proposal
from app.orchestrator.prompts import render
from app.schemas.negotiator import (
    CounterOffer,
    GuardResult,
    NegotiationEnvelope,
    NegotiationOutcome,
    NegotiationRequest,
    NegotiationResult,
    NegotiatorAction,
    NegotiatorDecision,
    NegotiatorPolicy,
    ProposalDecision,
    StopReason,
)

logger = get_logger(__name__)

_NODE = "negotiator"
_OUTCOME = {
    NegotiatorAction.ACCEPT: NegotiationOutcome.ACCEPT,
    NegotiatorAction.COUNTER: NegotiationOutcome.COUNTER,
    NegotiatorAction.STOP: NegotiationOutcome.STOP,
}

__all__ = ["negotiate"]


class _Rejected(Exception):
    """A deterministic check refused the output; the message is fed back for one retry."""


class _Unavailable(Exception):
    """The model could not produce an acceptable decision; the fail-safe is STOP."""


def _dump(value: object) -> str:
    return json.dumps(value, indent=1, default=str)


def _prompt(envelope: NegotiationEnvelope, request: NegotiationRequest, policy: NegotiatorPolicy) -> str:
    return render(
        "negotiator",
        trigger=request.trigger.value,
        round=str(request.round),
        max_rounds=str(policy.max_rounds),
        failed_criteria=_dump(list(request.failed_criteria)),
        baseline=_dump(envelope.baseline_offer.model_dump(mode="json")),
        envelope=_dump(
            {
                "max_total_minor": envelope.max_total_minor,
                "latest_acceptable_at": envelope.latest_acceptable_at.isoformat(),
                "non_negotiable_terms": envelope.non_negotiable_terms,
            }
        ),
        incoming=_dump(request.incoming.model_dump(mode="json") if request.incoming else None),
    )


def _candidate(envelope: NegotiationEnvelope, request: NegotiationRequest, decision: NegotiatorDecision) -> CounterOffer | None:
    """The offer the decision would put on the table; identity always comes from the approved deal."""
    match decision.action:
        case NegotiatorAction.ACCEPT:
            if request.incoming is None:
                raise _Rejected("there is no incoming offer to accept")
            return request.incoming
        case NegotiatorAction.COUNTER:
            assert decision.counter is not None  # guaranteed by NegotiatorDecision
            base = envelope.baseline_offer
            return CounterOffer(
                subject_id=base.subject_id, counterparty_id=base.counterparty_id, currency=base.currency,
                **dict(decision.counter),
            )
        case NegotiatorAction.STOP:
            return None


async def _decide(
    prompt: str,
    envelope: NegotiationEnvelope,
    request: NegotiationRequest,
    policy: NegotiatorPolicy,
    provider: LLMProvider | None,
    audit: AuditScope | None,
) -> tuple[NegotiatorDecision, CounterOffer | None, GuardResult | None]:
    current = prompt
    for attempt in range(1, policy.decision_attempts + 1):
        try:
            decision = await call(current, NegotiatorDecision, retries=policy.parse_retries, provider=provider)
        except LLMGatewayError as exc:
            logger.warning("negotiator_llm_failed", extra={"error": str(exc)})
            raise _Unavailable("model output unavailable or unparseable") from exc
        try:
            if decision.action is NegotiatorAction.COUNTER and request.round >= policy.max_rounds:
                raise _Rejected("final round: only accept or stop is allowed")
            offer = _candidate(envelope, request, decision)
            guard = check_proposal(envelope, offer) if offer else None
            if guard is not None and guard.decision is ProposalDecision.BLOCKED:
                _emit(audit, "NEGOTIATION_GUARD_CHECKED", request, decision=guard.decision.value, guard=guard)
                raise _Rejected(f"outside the envelope: {', '.join(v.value for v in guard.violations)}")
        except _Rejected as exc:
            logger.warning("negotiator_output_rejected", extra={"reason": str(exc)})
            _emit(audit, "NEGOTIATOR_REJECTED", request, decision="retry", reason=str(exc), attempt=attempt)
            current = f"{prompt}\n\nYour previous answer was rejected: {exc}. Answer again and fix this."
            continue
        _emit(audit, "NEGOTIATOR_DECIDED", request, decision=decision.action.value, reason=decision.rationale, attempt=attempt)
        if guard is not None:
            _emit(audit, "NEGOTIATION_GUARD_CHECKED", request, decision=guard.decision.value, guard=guard)
        return decision, offer, guard
    raise _Unavailable("model output rejected after retry")


async def negotiate(
    envelope: NegotiationEnvelope,
    request: NegotiationRequest,
    *,
    policy: NegotiatorPolicy | None = None,
    provider: LLMProvider | None = None,
    audit: AuditScope | None = None,
) -> NegotiationResult:
    """Never raises for model problems: the fail-safe is STOP (report to the user)."""
    policy = policy or NegotiatorPolicy()
    if audit:
        audit.emit(
            Phase.EXECUTION, "NEGOTIATION_STARTED", node=_NODE, actor_type="agent",
            payload=request.model_dump(mode="json"), metadata={"trigger": request.trigger.value, "round": request.round},
        )
    if request.round > policy.max_rounds:
        return _finish(audit, NegotiationResult(
            outcome=NegotiationOutcome.STOP, round=request.round, stop_reason=StopReason.ROUND_CAP,
            reasons=(f"round cap of {policy.max_rounds} reached",),
        ))
    try:
        decision, offer, guard = await _decide(_prompt(envelope, request, policy), envelope, request, policy, provider, audit)
    except _Unavailable as exc:
        return _finish(audit, NegotiationResult(
            outcome=NegotiationOutcome.STOP, round=request.round, stop_reason=StopReason.MODEL_UNAVAILABLE, reasons=(str(exc),),
        ))
    outcome = _OUTCOME[decision.action]
    return _finish(audit, NegotiationResult(
        outcome=outcome, round=request.round, offer=offer, guard=guard,
        stop_reason=StopReason.MODEL_STOPPED if outcome is NegotiationOutcome.STOP else None, reasons=(decision.rationale,),
    ))


def _emit(
    audit: AuditScope | None, event: str, request: NegotiationRequest, *, decision: str, reason: str | None = None,
    guard: GuardResult | None = None, **meta: object,
) -> None:
    if audit:
        if guard:
            meta |= {
                "violations": [v.value for v in guard.violations], "changed_fields": list(guard.changed_fields),
                "terms_hash": guard.terms_hash,
            }
        audit.emit(
            Phase.EXECUTION, event, node=_NODE, actor_type="agent", decision=decision, reason=reason,
            metadata={"round": request.round, **meta},
        )


def _finish(audit: AuditScope | None, result: NegotiationResult) -> NegotiationResult:
    if audit:
        audit.emit(
            Phase.EXECUTION, "NEGOTIATION_COMPLETED", node=_NODE, decision=result.outcome.value,
            reason="; ".join(result.reasons),
            metadata={
                "round": result.round, "stop_reason": result.stop_reason.value if result.stop_reason else None,
                "needs_fresh_approval": result.needs_fresh_approval,
                "terms_hash": result.guard.terms_hash if result.guard else None,
            },
        )
    return result
