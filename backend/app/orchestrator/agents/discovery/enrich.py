"""Step 3: enrich a candidate with details, reviews, stock and delivery through the registry.

Every call goes through ``ConnectorRegistry`` (authorization, text boundary, quality flags). Review
text is stored by the registry; only the text-free ``ReviewSummary`` comes back. A failed call leaves
the candidate as it was and is reported in ``errors``; it never fails the run.
"""

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime

from app.audit.events import AuditScope, Phase
from app.connectors.base import (
    CandidateRequest,
    ConnectorError,
    DeliveryRequest,
    ReviewsRequest,
)
from app.connectors.normalize import assess_quality
from app.connectors.registry import DEFAULT_MAX_AGE, ConnectorRegistry
from app.schemas.authorization import TaskAuthorization
from app.schemas.candidate import Candidate, DataQuality


async def _attempt[T](call: Awaitable[T] | None, errors: list[str]) -> T | None:
    if call is None:
        return None
    try:
        return await call
    except ConnectorError as exc:
        errors.append(str(exc))
        return None


def _merge_refs(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for group in groups for ref in group))


async def enrich_candidate(
    registry: ConnectorRegistry,
    auth: TaskAuthorization,
    candidate: Candidate,
    *,
    destination: str | None,
    reviews_limit: int,
    errors: list[str],
    audit: AuditScope | None = None,
) -> Candidate | None:
    """The enriched candidate, or ``None`` when the platform says it is out of stock."""
    source = candidate.source
    request = CandidateRequest(candidate_id=candidate.candidate_id)
    delivery_call = (
        registry.delivery(auth, source, DeliveryRequest(candidate_id=candidate.candidate_id, destination=destination))
        if destination
        else None
    )
    details, reviews, stock, delivery = await asyncio.gather(
        _attempt(registry.details(auth, source, request), errors),
        _attempt(
            registry.reviews(auth, source, ReviewsRequest(candidate_id=candidate.candidate_id, limit=reviews_limit)), errors
        ),
        _attempt(registry.stock(auth, source, request), errors),
        _attempt(delivery_call, errors),
    )
    if stock is not None and not stock.available:
        if audit:
            audit.emit(
                Phase.DISCOVERY, "CANDIDATE_UNAVAILABLE", node="discovery", resource_type="candidate",
                resource_id=candidate.candidate_id, decision="drop", metadata={"source": source},
            )
        return None

    merged = details or candidate
    refs = _merge_refs(candidate.text_refs, merged.text_refs)
    updates: dict[str, object] = {}
    if reviews is not None and reviews.review_count:
        refs = _merge_refs(refs, reviews.text_refs)
        if merged.rating is None and reviews.average_rating is not None:
            updates["rating"] = reviews.average_rating
        if merged.review_count is None:
            updates["review_count"] = reviews.review_count
        if merged.latest_review_at is None and reviews.latest_review_at is not None:
            updates["latest_review_at"] = reviews.latest_review_at
    if delivery is not None and delivery.available_by is not None and merged.available_by is None:
        updates["available_by"] = delivery.available_by

    enriched = merged.model_copy(update={**updates, "text_refs": refs})
    # A MISSING flag is obsolete once the field has a value; assess_quality re-flags whatever is still wrong.
    quality = {
        name: flag
        for name, flag in enriched.quality.items()
        if not (flag is DataQuality.MISSING and getattr(enriched, name, None) is not None)
    }
    result = assess_quality(enriched.model_copy(update={"quality": quality}), now=datetime.now(UTC), max_age=DEFAULT_MAX_AGE)
    if audit:
        audit.emit(
            Phase.DISCOVERY, "CANDIDATE_ENRICHED", node="discovery", resource_type="candidate",
            resource_id=candidate.candidate_id, decision="ok",
            metadata={
                "source": source, "filled": sorted(updates), "details": details is not None,
                "reviews": reviews is not None, "stock": stock is not None, "delivery": delivery is not None,
                "missing": sorted(n for n, f in result.quality.items() if f is DataQuality.MISSING),
            },
        )
    return result
