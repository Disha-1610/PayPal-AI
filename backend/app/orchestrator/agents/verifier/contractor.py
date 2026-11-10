"""Generator-critic verification of a contractor deliverable (contractor / A2A only).

  1. The generator maps the deliverable evidence to each agreed criterion (status + cited evidence ids).
  2. The critic independently re-checks every criterion and challenges unsupported claims.
  3. Disagreement or low confidence -> revise with the critic's challenges, at most ``max_passes`` rounds.
  4. Still unresolved, or the model is unavailable / unparseable -> ``INSUFFICIENT_EVIDENCE`` (human).

The model never produces a verdict: code reduces agreed per-criterion statuses (all met -> PASS, any not
met -> FAIL, otherwise INSUFFICIENT) and the result becomes ``Evidence.confirmed``. Deliverable text is
untrusted and only ever shown as quoted data, so an injected "approve this" cannot make a criterion pass:
a ``met`` must cite real evidence ids and survive an independent critic. Capture stays with the Executor.
"""

import json
from collections.abc import Callable, Sequence
from typing import TypeVar

from pydantic import BaseModel

from app.audit.events import AuditScope, Phase
from app.core.logging import get_logger
from app.llm.gateway import LLMGatewayError, call
from app.llm.providers import LLMProvider
from app.orchestrator.prompts import render
from app.schemas.recheck import ObservedTerms
from app.schemas.verification import Evidence, VerificationVerdict
from app.schemas.verifier import (
    CriterionFinding,
    CriterionStatus,
    CriticOutput,
    DeliverableItem,
    DeliverableVerdict,
    GeneratorOutput,
    VerifierPolicy,
)

logger = get_logger(__name__)

_NODE = "verifier"
T = TypeVar("T", bound=BaseModel)

__all__ = ["reduce_findings", "to_evidence", "verify_deliverable"]


class _Rejected(Exception):
    """Model output failed a deterministic check; the message is fed back for one retry."""


class _Unavailable(Exception):
    """The model could not produce valid output; the fail-safe is INSUFFICIENT_EVIDENCE."""


def _check(findings: Sequence[CriterionFinding], criteria_count: int, known_ids: set[str]) -> None:
    indices = [f.criterion_index for f in findings]
    if sorted(indices) != list(range(criteria_count)):
        raise _Rejected(f"need exactly one finding per criterion index 0..{criteria_count - 1}")
    for f in findings:
        unknown = set(f.evidence_ids) - known_ids
        if unknown:
            raise _Rejected(f"unknown evidence_id(s): {sorted(unknown)}")
        if f.status is CriterionStatus.MET and not f.evidence_ids:
            raise _Rejected(f"criterion {f.criterion_index} is met but cites no evidence")


def _statuses(findings: Sequence[CriterionFinding]) -> tuple[CriterionStatus, ...]:
    return tuple(f.status for f in sorted(findings, key=lambda f: f.criterion_index))


def reduce_findings(findings: Sequence[CriterionFinding]) -> VerificationVerdict:
    statuses = _statuses(findings)
    if all(s is CriterionStatus.MET for s in statuses):
        return VerificationVerdict.PASS
    if any(s is CriterionStatus.NOT_MET for s in statuses):
        return VerificationVerdict.FAIL
    return VerificationVerdict.INSUFFICIENT_EVIDENCE


async def _ask(
    prompt: str, model: type[T], policy: VerifierPolicy, provider: LLMProvider | None, check: Callable[[T], None]
) -> T:
    current = prompt
    for _ in range(policy.decision_attempts):
        try:
            result = await call(current, model, retries=policy.parse_retries, provider=provider)
        except LLMGatewayError as exc:
            logger.warning("verifier_llm_failed", extra={"error": str(exc)})
            raise _Unavailable("model output unavailable or unparseable") from exc
        try:
            check(result)
        except _Rejected as exc:
            logger.warning("verifier_output_rejected", extra={"reason": str(exc)})
            current = f"{prompt}\n\nYour previous answer was rejected: {exc}. Answer again and fix this."
            continue
        return result
    raise _Unavailable("model output rejected after retry")


def _feedback(critic: CriticOutput | None) -> str:
    if critic is None:
        return ""
    revised = json.dumps(
        {"critic_findings": [f.model_dump(mode="json") for f in critic.findings], "challenges": list(critic.challenges)}, indent=1
    )
    return f"\nA critic disagreed with your last answer. Reconsider every criterion (JSON):\n{revised}\n"


async def verify_deliverable(
    criteria: Sequence[str],
    deliverables: Sequence[DeliverableItem],
    *,
    policy: VerifierPolicy | None = None,
    provider: LLMProvider | None = None,
    audit: AuditScope | None = None,
) -> DeliverableVerdict:
    """Never raises for model problems: the fail-safe verdict is INSUFFICIENT_EVIDENCE (escalate to a human)."""
    policy = policy or VerifierPolicy()
    if not criteria:
        raise ValueError("a milestone needs at least one acceptance criterion")
    ids = [d.evidence_id for d in deliverables]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate evidence_id")
    if not deliverables:
        return _finish(
            audit, DeliverableVerdict(verdict=VerificationVerdict.INSUFFICIENT_EVIDENCE, passes=0, reasons=("no deliverable evidence",))
        )

    known = set(ids)
    criteria_json = json.dumps(list(criteria), indent=1)
    items_json = json.dumps(
        [{"evidence_id": d.evidence_id, "kind": d.kind, "content": d.content[: policy.max_item_chars]} for d in deliverables],
        indent=1,
    )

    def check(output: GeneratorOutput | CriticOutput) -> None:
        _check(output.findings, len(criteria), known)

    critic: CriticOutput | None = None
    try:
        for n in range(1, policy.max_passes + 1):
            proposal = await _ask(
                render("verifier_generate", criteria=criteria_json, deliverables=items_json, feedback=_feedback(critic)),
                GeneratorOutput, policy, provider, check,
            )
            _emit(audit, "VERIFIER_GENERATOR_PASS", n, decision=reduce_findings(proposal.findings).value, confidence=proposal.confidence)
            proposal_json = json.dumps([f.model_dump(mode="json") for f in proposal.findings], indent=1)
            critic = await _ask(
                render("verifier_critic", criteria=criteria_json, deliverables=items_json, proposal=proposal_json),
                CriticOutput, policy, provider, check,
            )
            agreed = _statuses(proposal.findings) == _statuses(critic.findings)
            confident = min(proposal.confidence, critic.confidence) >= policy.min_confidence
            _emit(
                audit, "VERIFIER_CRITIC_PASS", n, decision="agree" if agreed else "disagree", confidence=critic.confidence,
                reason="; ".join(critic.challenges), challenges=len(critic.challenges),
            )
            if agreed and confident:
                reasons = tuple(
                    f"criterion {f.criterion_index}: {f.status.value}" for f in critic.findings if f.status is not CriterionStatus.MET
                )
                return _finish(
                    audit,
                    DeliverableVerdict(verdict=reduce_findings(critic.findings), passes=n, reasons=reasons, findings=critic.findings),
                )
    except _Unavailable as exc:
        return _finish(audit, DeliverableVerdict(verdict=VerificationVerdict.INSUFFICIENT_EVIDENCE, passes=0, reasons=(str(exc),)))
    return _finish(
        audit,
        DeliverableVerdict(
            verdict=VerificationVerdict.INSUFFICIENT_EVIDENCE, passes=policy.max_passes,
            reasons=("generator and critic did not agree with sufficient confidence",),
        ),
    )


def _emit(
    audit: AuditScope | None, event: str, n: int, *, decision: str, confidence: float, reason: str | None = None, **meta: object
) -> None:
    if audit:
        audit.emit(
            Phase.EXECUTION, event, node=_NODE, actor_type="agent", decision=decision, reason=reason or None,
            metadata={"pass": n, "confidence": confidence, **meta},
        )


def _finish(audit: AuditScope | None, result: DeliverableVerdict) -> DeliverableVerdict:
    if audit:
        audit.emit(
            Phase.EXECUTION, "VERIFIER_COMPLETED", node=_NODE, decision=result.verdict.value, reason="; ".join(result.reasons),
            metadata={"passes": result.passes, "confirmed": result.confirmed},
        )
    return result


def to_evidence(source: str, observed: ObservedTerms, verdict: DeliverableVerdict) -> Evidence:
    """The only place model output reaches settlement: a boolean. ``verify_evidence`` still checks the terms."""
    return Evidence(source=source, observed=observed, confirmed=verdict.confirmed)
