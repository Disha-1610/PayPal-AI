"""Discovery agent: one step per node, resumable through ``TaskState.discovery``.

Nodes (wire each with ``functools.partial``; none calls another or the router):
  discovery_start_node    fresh ``DiscoveryProgress`` (first run, or when Adapt / the user loop re-runs discovery)
  discovery_search_node   one search round on the current platform: search_resilient -> dedupe -> enrich top N -> thin check
  discovery_adapt_node    pick the next query from the ladder (synonyms, drop soft term, widen category)
  discovery_advance_node  move to the next platform
  discovery_finish_node   record the stop reason and the status

After every node the graph's conditional edge calls ``router.discovery.route_discovery(state["discovery"])``
and goes to the node it names. The agent only reports (candidates, report, flags); the Router decides.
Platforms are searched one at a time with an authorization narrowed to that platform, so scope only shrinks.
The LLM only proposes query text (see ``rewrite``) and never takes part in routing.
"""

import asyncio

from app.audit.events import AuditScope, Phase
from app.connectors.base import Capability
from app.connectors.registry import ConnectorRegistry
from app.domains.base import DomainPlugin
from app.llm.providers import LLMProvider
from app.orchestrator.agents.discovery.enrich import enrich_candidate
from app.orchestrator.agents.discovery.request import build_search_request, initial_plan
from app.orchestrator.agents.discovery.rewrite import QueryAdapter, QueryRewriter
from app.orchestrator.agents.discovery.thin import assess_thinness
from app.orchestrator.progress.hashing import progress_hash
from app.orchestrator.router.discovery import stop_reason
from app.schemas.authorization import TaskAuthorization
from app.schemas.candidate import Candidate
from app.schemas.discovery import DiscoveryPolicy, DiscoveryProgress, LadderState
from app.schemas.state import TaskState

DISCOVERY_WRITABLE = frozenset({"candidates", "discovery", "status", "errors"})


def _key(candidate: Candidate) -> str:
    return f"{candidate.source}:{candidate.candidate_id}"


def searchable_platforms(registry: ConnectorRegistry, auth: TaskAuthorization, domain: str) -> tuple[str, ...]:
    """Permitted platforms of the domain that can search, in registration order (stable across a resume)."""
    return tuple(
        c.id
        for c in registry.scoped(auth, domain)
        if any(spec.name == Capability.SEARCH.value for spec in c.capabilities)
    )


def discovery_start_node(state: TaskState, *, audit: AuditScope | None = None) -> dict[str, object]:
    """Reset the loop. The candidate pool is kept: re-found candidates replace their stale copies."""
    if audit:
        audit.emit(Phase.DISCOVERY, "DISCOVERY_STARTED", node="discovery", decision="start")
    return {"discovery": DiscoveryProgress(), "status": "discovering"}


async def discovery_search_node(
    state: TaskState,
    *,
    registry: ConnectorRegistry,
    plugin: DomainPlugin,
    destination: str | None = None,
    policy: DiscoveryPolicy | None = None,
    audit: AuditScope | None = None,
) -> dict[str, object]:
    policy = policy or DiscoveryPolicy()
    progress = state.get("discovery") or DiscoveryProgress()
    auth = state["authorization"]
    constraints = state.get("constraints", [])
    platforms = searchable_platforms(registry, auth, plugin.name)
    pool = {_key(c): c for c in state.get("candidates", [])}

    if progress.platform_index >= len(platforms):  # nothing in scope can search
        report = assess_thinness(tuple(pool.values()), failed_sources=(), policy=policy)
        done = progress.model_copy(update={"plan_searched": True, "ladder_exhausted": True, "report": report})
        return {"discovery": done, "status": "discovering"}

    platform = platforms[progress.platform_index]
    platform_auth = auth.model_copy(update={"permitted_platforms": frozenset({platform})})
    plan = progress.plan or initial_plan(state.get("goal", ""), constraints, plugin)
    request = build_search_request(plan, constraints, plugin, limit=policy.search_limit).model_copy(
        update={"currency": auth.currency}
    )
    result = await registry.search_resilient(platform_auth, request, max_attempts=policy.max_search_attempts, audit=audit)
    errors = list(result.errors)

    enriched = set(progress.enriched)
    unavailable = set(progress.unavailable)
    # Downstream (Evaluator, selection, approval) identifies a candidate by ``candidate_id`` alone, so an id another
    # platform already uses would make two different offers indistinguishable. The first one found keeps the id.
    owner = {c.candidate_id: c.source for c in pool.values()}
    clashes = [c for c in result.candidates if owner.get(c.candidate_id, c.source) != c.source]
    for clash in clashes:
        errors.append(f"{clash.source}: candidate id {clash.candidate_id!r} is already used by {owner[clash.candidate_id]}; skipped")
        if audit:
            audit.emit(
                Phase.DISCOVERY, "CANDIDATE_ID_COLLISION", node="discovery", resource_type="candidate",
                resource_id=clash.candidate_id, decision="skip", metadata={"source": clash.source, "kept": owner[clash.candidate_id]},
            )
    found = [c for c in result.candidates if _key(c) not in unavailable and c not in clashes]
    for candidate in found:  # dedupe by (source, id); an already-enriched record is richer, keep it
        if _key(candidate) not in enriched:
            pool[_key(candidate)] = candidate
    to_enrich = [c for c in found if _key(c) not in enriched][: policy.enrich_top_n]
    enriched_records = await asyncio.gather(  # independent candidates: enrich concurrently, results keep input order
        *(
            enrich_candidate(
                registry, platform_auth, candidate, destination=destination, reviews_limit=policy.reviews_limit,
                errors=errors, audit=audit,
            )
            for candidate in to_enrich
        )
    )
    for candidate, updated in zip(to_enrich, enriched_records, strict=True):
        enriched.add(_key(candidate))
        if updated is None:
            pool.pop(_key(candidate), None)
            unavailable.add(_key(candidate))
        else:
            pool[_key(candidate)] = updated

    digest = progress_hash(pool, constraints)
    no_progress = not progress.first_round and digest in progress.seen_hashes
    report = assess_thinness(tuple(pool.values()), failed_sources=result.failed_sources, policy=policy)
    updated_progress = progress.model_copy(
        update={
            "plan": plan, "plan_searched": True, "first_round": False, "no_progress": no_progress,
            "rounds": progress.rounds + 1, "queries": (*progress.queries, plan.text),
            "seen_hashes": (*progress.seen_hashes, digest), "enriched": tuple(sorted(enriched)),
            "unavailable": tuple(sorted(unavailable)), "report": report,
        }
    )
    if audit:
        audit.emit(
            Phase.DISCOVERY, "DISCOVERY_ROUND_COMPLETED", node="discovery", resource_type="connector", resource_id=platform,
            decision="thin" if report.thin else "ok",
            metadata={
                "round": updated_progress.rounds, "query": plan.text, "total": report.total, "complete": report.complete,
                "reasons": [r.value for r in report.reasons], "no_progress": no_progress,
                "failed_sources": list(result.failed_sources),
            },
        )
    return {
        "candidates": list(pool.values()), "discovery": updated_progress, "status": "discovering", "errors": errors,
    }


async def discovery_adapt_node(
    state: TaskState,
    *,
    plugin: DomainPlugin,
    policy: DiscoveryPolicy | None = None,
    provider: LLMProvider | None = None,
    audit: AuditScope | None = None,
) -> dict[str, object]:
    """Choose the next query. Makes no connector calls; the model is asked only for rewrite text."""
    policy = policy or DiscoveryPolicy()
    progress = state["discovery"]
    constraints = state.get("constraints", [])
    base = initial_plan(state.get("goal", ""), constraints, plugin)
    rewriter = QueryRewriter(plugin, constraints, cache=progress.rewrite_cache, provider=provider, audit=audit)
    adapter = QueryAdapter(base, rewriter, policy, progress.ladder if progress.ladder.tried else None)
    adapted = await adapter.next()
    if audit:
        audit.emit(
            Phase.DISCOVERY, "QUERY_ADAPTED", node="discovery", actor_type="agent",
            decision=adapted.rung.value if adapted else "exhausted",
            metadata={"query": adapted.plan.text if adapted else None, "platform_index": progress.platform_index},
        )
    update: dict[str, object] = {"ladder": adapter.state, "rewrite_cache": rewriter.cache}
    if adapted is None:
        update["ladder_exhausted"] = True
    else:
        update.update(plan=adapted.plan, plan_searched=False)
    return {"discovery": progress.model_copy(update=update)}


def discovery_advance_node(state: TaskState, *, audit: AuditScope | None = None) -> dict[str, object]:
    """Next platform: fresh ladder and base query; pool, hashes, rounds and the rewrite cache carry over."""
    progress = state["discovery"]
    if audit:
        audit.emit(
            Phase.DISCOVERY, "PLATFORM_ADVANCED", node="discovery", decision="next_platform",
            metadata={"from_index": progress.platform_index, "reasons": [r.value for r in progress.report.reasons]},
        )
    return {
        "discovery": progress.model_copy(
            update={
                "platform_index": progress.platform_index + 1, "plan": None, "plan_searched": False, "first_round": True,
                "no_progress": False, "ladder": LadderState(), "ladder_exhausted": False,
            }
        )
    }


def discovery_finish_node(
    state: TaskState, *, policy: DiscoveryPolicy | None = None, audit: AuditScope | None = None
) -> dict[str, object]:
    policy = policy or DiscoveryPolicy()
    progress = state["discovery"]
    stop = stop_reason(progress, policy)
    candidates = state.get("candidates", [])
    if audit:
        audit.emit(
            Phase.DISCOVERY, "DISCOVERY_COMPLETED", node="discovery", decision=stop.value,
            metadata={
                "candidates": len(candidates), "rounds": progress.rounds, "queries": list(progress.queries),
                "unavailable": len(progress.unavailable),
            },
        )
    return {
        "discovery": progress.model_copy(update={"stop": stop}),
        "status": "discovered" if candidates else "discovery_empty",
    }
