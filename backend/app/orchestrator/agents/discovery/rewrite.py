"""Step 5: the query-adaptation ladder. Fixed order: synonyms, then drop a soft term, then widen the category.

The LLM only proposes replacement query text (rungs 1 and 3); dropping a soft term is plain code.
Output is the strict ``QueryRewrites`` schema (the gateway retries parse failures). Every proposal
then passes ``guard.query_guard.validate_rewrite``; if the guard rejects all of them the model gets
one corrective retry with the reasons. A model failure skips that rung (audited); it never stops
discovery.

State is resumable: the ladder position is a ``LadderState`` and validated rewrites live in a cache
that the node persists, so a resumed run does not ask the model again or get different queries.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.audit.events import AuditScope, Phase
from app.core.logging import get_logger
from app.domains.base import DomainPlugin
from app.llm.gateway import LLMGatewayError, call
from app.llm.providers import LLMProvider
from app.orchestrator.guard.query_guard import (
    QueryRejected,
    excluded_terms,
    normalize_query,
    validate_rewrite,
)
from app.orchestrator.prompts import render
from app.schemas.constraints import Constraint
from app.schemas.discovery import (
    DiscoveryPolicy,
    LadderState,
    QueryPlan,
    QueryRewrites,
    QueryRung,
)

logger = get_logger(__name__)

_INSTRUCTIONS = {
    QueryRung.SYNONYMS: "Rewrite the query using different words for the same item (synonyms or alternative names).",
    QueryRung.WIDEN_CATEGORY: "Give a broader category that contains the item, so that more results match.",
}


@dataclass(frozen=True, slots=True)
class AdaptedQuery:
    rung: QueryRung
    plan: QueryPlan


class QueryRewriter:
    """Asks the model for rewrites, guards them, and caches the validated result per (rung, query)."""

    def __init__(
        self,
        plugin: DomainPlugin,
        constraints: Iterable[Constraint],
        *,
        cache: dict[str, tuple[str, ...]] | None = None,
        provider: LLMProvider | None = None,
        audit: AuditScope | None = None,
    ) -> None:
        self._domain = plugin.name
        self._constraints = list(constraints)
        self._provider = provider
        self._audit = audit
        self.cache: dict[str, tuple[str, ...]] = dict(cache or {})

    async def propose(self, rung: QueryRung, query: str, *, count: int, tried: set[str]) -> tuple[str, ...]:
        key = f"{rung.value}|{query}"
        if key not in self.cache:
            self.cache[key] = await self._validated(rung, query, count, tried)
        return self.cache[key][:count]

    async def _validated(self, rung: QueryRung, query: str, count: int, tried: set[str]) -> tuple[str, ...]:
        feedback = ""
        for attempt in (1, 2):
            proposals = await self._ask(rung, query, count, feedback)
            if proposals is None:
                return ()
            accepted: list[str] = []
            reasons: list[str] = []
            for proposed in proposals:
                try:
                    accepted.append(validate_rewrite(proposed, tried={*tried, *accepted}, constraints=self._constraints))
                except QueryRejected as exc:
                    reasons.append(f"{proposed[:40]!r}: {exc}")
                    self._emit("QUERY_REWRITE_REJECTED", rung, str(exc), {"proposed": proposed[:80], "attempt": attempt})
            if accepted or not reasons:
                self._emit("QUERY_REWRITE_PROPOSED", rung, None, {"accepted": accepted, "attempt": attempt}, decision="ok")
                return tuple(accepted)
            feedback = "Your previous answer was rejected: " + "; ".join(reasons) + ". Answer again and fix this."
        return ()

    async def _ask(self, rung: QueryRung, query: str, count: int, feedback: str) -> tuple[str, ...] | None:
        prompt = render(
            "query_rewrite",
            domain=self._domain,
            query=query,
            instruction=_INSTRUCTIONS[rung],
            count=str(count),
            excluded=", ".join(excluded_terms(self._constraints)) or "(none)",
        )
        try:
            output = await call(f"{prompt}\n{feedback}" if feedback else prompt, QueryRewrites, provider=self._provider)
        except LLMGatewayError as exc:
            logger.warning("query_rewrite_unavailable", extra={"rung": rung.value, "error": str(exc)})
            self._emit("QUERY_REWRITE_UNAVAILABLE", rung, "model call failed; rung skipped", {})
            return None
        return tuple(output.queries)

    def _emit(
        self, event: str, rung: QueryRung, reason: str | None, metadata: dict[str, object], *, decision: str = "skip"
    ) -> None:
        if self._audit:
            self._audit.emit(
                Phase.DISCOVERY, event, node="discovery", actor_type="agent", decision=decision, reason=reason,
                metadata={"rung": rung.value, **metadata},
            )


class QueryAdapter:
    """Yields the next untried query for one platform, walking the rungs in order. ``state`` is persistable."""

    _ORDER = (QueryRung.SYNONYMS, QueryRung.DROP_SOFT_TERM, QueryRung.WIDEN_CATEGORY)

    def __init__(
        self, base: QueryPlan, rewriter: QueryRewriter, policy: DiscoveryPolicy, state: LadderState | None = None
    ) -> None:
        self._base = base
        self._rewriter = rewriter
        self._policy = policy
        state = state or LadderState(tried=(normalize_query(base.text),))
        self._rung = state.rung_index
        self._loaded = state.loaded
        self._queue = list(state.queue)
        self._tried = set(state.tried)

    @property
    def state(self) -> LadderState:
        return LadderState(
            rung_index=self._rung, loaded=self._loaded, queue=tuple(self._queue), tried=tuple(sorted(self._tried))
        )

    async def _load(self, rung: QueryRung) -> list[QueryPlan]:
        policy, base = self._policy, self._base
        if rung is QueryRung.DROP_SOFT_TERM:
            drops = range(1, min(policy.max_soft_drops, len(base.soft_terms)) + 1)
            return [base.drop_soft(n) for n in drops]
        count = policy.max_synonyms if rung is QueryRung.SYNONYMS else 1
        if count == 0:
            return []
        proposals = await self._rewriter.propose(rung, base.core, count=count, tried=self._tried)
        soft = base.soft_terms if rung is QueryRung.SYNONYMS else ()  # a wider category has no soft terms left
        return [QueryPlan(core=text, soft_terms=soft) for text in proposals]

    async def next(self) -> AdaptedQuery | None:
        while self._rung < len(self._ORDER):
            rung = self._ORDER[self._rung]
            if not self._loaded:
                self._queue, self._loaded = await self._load(rung), True
            while self._queue:
                plan = self._queue.pop(0)
                text = normalize_query(plan.text)
                if text not in self._tried:
                    self._tried.add(text)
                    return AdaptedQuery(rung, plan)
            self._rung, self._loaded = self._rung + 1, False
        return None
