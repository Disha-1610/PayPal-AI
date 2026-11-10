"""Deterministic verification of delivered evidence against approved terms.

No LLM is involved: money is captured only on a PASS computed here. Qualitative judgement (e.g. a
contractor deliverable) must be reduced to ``Evidence.confirmed`` by a trusted evaluator or a human.
"""

from app.orchestrator.executor.mismatch import MismatchPolicy, classify_mismatch
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.recheck import MismatchAction
from app.schemas.verification import (
    Evidence,
    VerificationResult,
    VerificationSpec,
    VerificationVerdict,
)


def verify_evidence(terms: ApprovalTerms, evidence: Evidence, spec: VerificationSpec) -> VerificationResult:
    report = classify_mismatch(terms, evidence.observed, MismatchPolicy(tolerated_fields=spec.tolerated_fields))
    reasons = tuple(f"{c.field}: {c.kind.value}" for c in report.changes if c.action is not MismatchAction.PROCEED)

    if evidence.confirmed is False:
        return VerificationResult(verdict=VerificationVerdict.FAIL, reasons=("counterparty rejected", *reasons), report=report)
    if report.action >= MismatchAction.DEADLINE_MISSED:  # deadline missed, changed terms or unavailable
        return VerificationResult(verdict=VerificationVerdict.FAIL, reasons=reasons, report=report)
    if report.action is MismatchAction.INSUFFICIENT_DATA or evidence.confirmed is None:
        pending = (*reasons, "fulfilment not yet confirmed") if evidence.confirmed is None else reasons
        return VerificationResult(verdict=VerificationVerdict.INSUFFICIENT_EVIDENCE, reasons=pending, report=report)
    return VerificationResult(verdict=VerificationVerdict.PASS, reasons=reasons, report=report)
