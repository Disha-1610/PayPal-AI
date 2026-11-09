"""Normalization applied to every connector result: attribute validation and data-quality flags."""

from datetime import datetime, timedelta

from app.connectors.base import ConnectorError
from app.domains import get_plugin
from app.domains.base import ValueType
from app.schemas.candidate import Candidate, DataQuality

_CORE_FIELDS = ("total_cost", "rating", "review_count", "available_by")


def _matches(value_type: ValueType, value: object) -> bool:
    if value_type is ValueType.BOOLEAN:
        return isinstance(value, bool)
    if value_type is ValueType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type is ValueType.TEXT:
        return isinstance(value, str)
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def validate_attributes(candidate: Candidate) -> None:
    """Candidate attributes must be fields of its domain plug-in, with the declared type."""
    try:
        plugin = get_plugin(candidate.domain)
    except ValueError as exc:
        raise ConnectorError(f"candidate {candidate.candidate_id}: {exc}") from exc
    for name, value in candidate.attributes.items():
        spec = plugin.field(name)
        if spec is None:
            raise ConnectorError(f"candidate {candidate.candidate_id}: unknown {candidate.domain} attribute {name!r}")
        if not _matches(spec.value_type, value):
            raise ConnectorError(
                f"candidate {candidate.candidate_id}: attribute {name!r} is not {spec.value_type.value}"
            )


def assess_quality(candidate: Candidate, *, now: datetime, max_age: timedelta) -> Candidate:
    """Add ``missing`` / ``stale`` / ``unreliable`` flags. Flags set by the connector are kept."""
    quality = dict(candidate.quality)
    for field in _CORE_FIELDS:
        if getattr(candidate, field) is None:
            quality.setdefault(field, DataQuality.MISSING)
    if candidate.rating is not None and not candidate.review_count:
        quality.setdefault("rating", DataQuality.UNRELIABLE)  # a rating with no reviews behind it
    if now - candidate.fetched_at > max_age:
        for field in _CORE_FIELDS:
            quality.setdefault(field, DataQuality.STALE)
    return candidate.model_copy(update={"quality": quality})
