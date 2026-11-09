"""Phase 4 graph nodes: ``adapt_node`` (decide the next rung) and ``ask_user_node`` (pause, then apply the reply).

Wire with ``functools.partial`` for ``plugin`` / ``policy`` / ``provider``. Routing after ``adapt_node``
follows ``state["adapt"].next_step``; after ``ask_user_node`` the graph resumes at discovery / evaluation.
Nodes return partial ``TaskState`` updates limited to ``ADAPT_WRITABLE`` (see ``validate_state_update``).
"""

from datetime import datetime

from langgraph.types import interrupt

from app.domains.base import DomainPlugin
from app.llm.providers import LLMProvider
from app.orchestrator.agents.adapt.diagnosis import diagnose, summarize
from app.orchestrator.agents.adapt.recovery import plan_adaptation, terminate
from app.orchestrator.agents.adapt.relaxation import apply_proposal
from app.orchestrator.agents.intent.agent import patch_intent, to_state_update
from app.schemas.adapt import (
    AdaptPolicy,
    AdaptProgress,
    RecoveryStep,
    RelaxationProposal,
    TerminationReason,
    UserDecision,
    UserDecisionKind,
)
from app.schemas.state import TaskState

ADAPT_WRITABLE = frozenset(
    {"adapt", "relaxation_proposals", "constraints", "constraint_diffs", "goal", "status", "questions"}
)

_STATUS_BY_STEP = {RecoveryStep.STOP: "failed", RecoveryStep.ASK_USER: "awaiting_user"}


def adapt_node(
    state: TaskState,
    *,
    plugin: DomainPlugin,
    now: datetime,
    policy: AdaptPolicy | None = None,
    excluded_ids: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Record the failed attempt and choose the next rung, or stop. Makes no external calls."""
    decision = plan_adaptation(
        state.get("candidates", []),
        state.get("constraints", []),
        state.get("adapt") or AdaptProgress(),
        now=now,
        policy=policy,
        source_fields=plugin.source_fields,
        excluded_ids=excluded_ids,
    )
    step = decision.progress.next_step
    return {
        "adapt": decision.progress,
        "relaxation_proposals": list(decision.proposals),
        "status": _STATUS_BY_STEP.get(step, "adapting") if step else "adapting",
    }


def _request_payload(proposals: list[RelaxationProposal]) -> dict[str, object]:
    lines = [f"{i}. {p.description}" for i, p in enumerate(proposals, start=1)]
    message = (
        "I could not find an option that meets all of your requirements. "
        "Which change, if any, would you accept?\n" + "\n".join(lines)
    )
    return {
        "type": "relaxation_request",
        "message": message,
        "proposals": [
            {"proposal_id": p.proposal_id, "description": p.description, "unlocks": len(p.unlocked_ids)}
            for p in proposals
        ],
    }


def parse_decision(raw: object) -> UserDecision:
    """Validate the resume value: a structured decision, or free text treated as a reply."""
    if isinstance(raw, UserDecision):
        return raw
    if isinstance(raw, str):
        return UserDecision(kind=UserDecisionKind.REPLY, text=raw)
    return UserDecision.model_validate(raw)


async def resolve_decision(
    decision: UserDecision,
    state: TaskState,
    *,
    plugin: DomainPlugin,
    provider: LLMProvider | None = None,
) -> dict[str, object]:
    """Turn the user's answer into a state update. Only here do hard constraints change, always user-sourced."""
    progress = state.get("adapt") or AdaptProgress()
    constraints = state.get("constraints", [])
    cleared: dict[str, object] = {"relaxation_proposals": []}

    if decision.kind is UserDecisionKind.CANCEL:
        return {**cleared, "adapt": terminate(progress, TerminationReason.CANCELLED, "cancelled by the user"), "status": "cancelled"}

    if decision.kind is UserDecisionKind.DECLINE:
        detail = summarize(diagnose(state.get("candidates", []), constraints))
        return {**cleared, "adapt": terminate(progress, TerminationReason.USER_DECLINED, detail), "status": "failed"}

    if decision.kind is UserDecisionKind.ACCEPT:
        updated, diff, _ = apply_proposal(decision, state.get("relaxation_proposals", []), constraints)
        return {**cleared, "constraints": updated, "constraint_diffs": [diff], "status": "adapting"}

    assert decision.text is not None  # guaranteed by UserDecision validation for REPLY
    result, diff, _ = await patch_intent(decision.text, state.get("goal", ""), list(constraints), plugin, provider=provider)
    return {**cleared, **to_state_update(result, diff)}


async def ask_user_node(
    state: TaskState, *, plugin: DomainPlugin, provider: LLMProvider | None = None
) -> dict[str, object]:
    """Pause for the user. Nothing with side effects may run above ``interrupt``: LangGraph re-runs this node on resume."""
    raw = interrupt(_request_payload(state.get("relaxation_proposals", [])))
    return await resolve_decision(parse_decision(raw), state, plugin=plugin, provider=provider)
