"""Deterministic routing for discovery. A pure function of the persisted ``DiscoveryProgress``.

The discovery nodes each do one step and return; this module alone decides what runs next, so the
model never takes part in routing. ``route_discovery`` is meant to be the conditional-edge function
after every discovery node.
"""

from app.audit.events import AuditScope, Phase
from app.schemas.discovery import (
    DiscoveryPolicy,
    DiscoveryProgress,
    DiscoveryRoute,
    StopReason,
)


def route_discovery(
    progress: DiscoveryProgress, *, platform_count: int, policy: DiscoveryPolicy, audit: AuditScope | None = None
) -> DiscoveryRoute:
    route = _decide(progress, platform_count, policy)
    if audit:
        audit.emit(
            Phase.DISCOVERY, "DISCOVERY_ROUTED", node="router", decision=route.value,
            metadata={
                "platform_index": progress.platform_index, "rounds": progress.rounds, "total": progress.report.total,
                "complete": progress.report.complete, "reasons": [r.value for r in progress.report.reasons],
                "no_progress": progress.no_progress, "ladder_exhausted": progress.ladder_exhausted,
            },
        )
    return route


def _decide(progress: DiscoveryProgress, platform_count: int, policy: DiscoveryPolicy) -> DiscoveryRoute:
    capped = progress.rounds >= policy.max_rounds
    if not progress.plan_searched:
        return DiscoveryRoute.EVALUATE if capped else DiscoveryRoute.SEARCH
    report = progress.report
    if not report.thin or capped:
        return DiscoveryRoute.EVALUATE
    if report.query_fixable and not progress.no_progress and not progress.ladder_exhausted:
        return DiscoveryRoute.ADAPT_QUERY
    if progress.platform_index + 1 < platform_count:
        return DiscoveryRoute.NEXT_PLATFORM
    return DiscoveryRoute.EVALUATE


def stop_reason(progress: DiscoveryProgress, policy: DiscoveryPolicy) -> StopReason:
    if not progress.report.thin:
        return StopReason.SUFFICIENT
    return StopReason.ROUND_CAP if progress.rounds >= policy.max_rounds else StopReason.EXHAUSTED
