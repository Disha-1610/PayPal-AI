"""Optional LLM layer: scores review text and listing prose into the fixed ``LLMScores`` schema.

The model sees untrusted text and nothing else: no tools, no state, no routing power. Output is
validated by the gateway (strict schema, retry) and again here (ids must be ones that were sent).
``None`` means "use code only"; this module never raises for bad model output or missing text.
"""

import json
from collections.abc import Sequence

from pydantic import BaseModel

from app.core.logging import get_logger
from app.llm.gateway import LLMGatewayError, call
from app.llm.providers import LLMProvider
from app.orchestrator.prompts import render
from app.schemas.candidate import Candidate
from app.schemas.evaluator import (
    CandidateScore,
    EvaluatorPolicy,
    LLMScores,
    TextSnippet,
    TextSource,
)

logger = get_logger(__name__)

NO_TEXT = "no text to score"


class _PromptCandidate(BaseModel):
    candidate_id: str
    title: str
    rating: float | None
    review_count: int | None
    texts: tuple[dict[str, object], ...]


class _Rejected(Exception):
    """Model output failed a deterministic check; the message is fed back for one retry."""


async def _snippets(
    source: TextSource, candidates: Sequence[Candidate], policy: EvaluatorPolicy
) -> dict[str, list[TextSnippet]] | None:
    refs = [ref for c in candidates for ref in c.text_refs]
    if not refs:
        return {c.candidate_id: [] for c in candidates}
    try:
        loaded = {s.ref: s for s in await source.load(refs)}
    except Exception:
        logger.warning("evaluator_text_load_failed", exc_info=True)
        return None
    return {c.candidate_id: [loaded[r] for r in c.text_refs if r in loaded][: policy.max_snippets] for c in candidates}


def _validate(result: LLMScores, expected: set[str]) -> dict[str, CandidateScore]:
    ids = [s.candidate_id for s in result.scores]
    if len(ids) != len(set(ids)):
        raise _Rejected("duplicate candidate_id in answer")
    unknown = set(ids) - expected
    if unknown:
        raise _Rejected(f"unknown candidate_id(s): {sorted(unknown)}")
    return {s.candidate_id: s for s in result.scores}


async def score_with_llm(
    candidates: Sequence[Candidate],
    text_source: TextSource,
    policy: EvaluatorPolicy,
    provider: LLMProvider | None,
) -> tuple[dict[str, CandidateScore] | None, str]:
    """Return ``(scores, why)``. ``scores`` is None on skip or failure; ``why`` says which."""
    texts = await _snippets(text_source, candidates, policy)
    if texts is None:
        return None, "text store unavailable"
    payload = [
        _PromptCandidate(
            candidate_id=c.candidate_id,
            title=c.title,
            rating=c.rating,
            review_count=c.review_count,
            texts=tuple(
                {"kind": s.kind, "rating": s.rating, "text": s.body[: policy.max_snippet_chars]}
                for s in texts[c.candidate_id]
            ),
        )
        for c in candidates
        if texts[c.candidate_id]
    ]
    if not payload:
        return None, NO_TEXT
    expected = {p.candidate_id for p in payload}
    prompt = render("evaluator_score", candidates=json.dumps([p.model_dump(mode="json") for p in payload], indent=1))
    current, why = prompt, ""
    for _ in range(policy.decision_attempts):
        try:
            result = await call(current, LLMScores, retries=policy.parse_retries, provider=provider)
        except LLMGatewayError as exc:
            logger.warning("evaluator_llm_failed", extra={"error": str(exc)})
            return None, "model output unavailable or unparseable"
        try:
            return _validate(result, expected), ""
        except _Rejected as exc:
            why = str(exc)
            logger.warning("evaluator_output_rejected", extra={"reason": why})
            current = f"{prompt}\n\nYour previous answer was rejected: {why}. Answer again and fix this."
    return None, f"model output rejected after retry: {why}"
