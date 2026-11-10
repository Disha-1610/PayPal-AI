"""Step 4: is the data thin? Pure function over the candidate pool."""

from collections.abc import Collection, Iterable

from app.schemas.candidate import Candidate
from app.schemas.discovery import DiscoveryPolicy, ThinReason, ThinReport


def is_complete(candidate: Candidate, required_fields: Iterable[str]) -> bool:
    return all(getattr(candidate, name) is not None for name in required_fields)


def assess_thinness(
    candidates: Collection[Candidate], *, failed_sources: Collection[str], policy: DiscoveryPolicy
) -> ThinReport:
    complete = sum(is_complete(c, policy.required_fields) for c in candidates)
    reasons: list[ThinReason] = []
    if len(candidates) < policy.min_candidates:
        reasons.append(ThinReason.TOO_FEW)
    elif complete < policy.min_candidates:
        reasons.append(ThinReason.MISSING_FIELDS)
    if failed_sources:
        reasons.append(ThinReason.PLATFORM_FAILURE)
    return ThinReport(reasons=tuple(reasons), total=len(candidates), complete=complete)
