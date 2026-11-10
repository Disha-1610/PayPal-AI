"""Coordinator agent: picks one action when no candidate is viable or a deadline is at risk.

It is called only by the Router, on structured state (never raw untrusted text), and returns a
``CoordinatorResult``; it never calls another agent, and the Router executes what it chose.
The model's output is a closed, strictly validated choice. The ``PlanEdit`` list is built by code
from that choice and applied with ``replan``. Safeguards: an iteration cap, a ``progress_hash``
no-progress stop, and a deterministic ASK_USER fallback when the output cannot be made valid.
"""

from dataclasses import dataclass, replace

from app.audit.events import AuditScope, Phase
from app.core.logging import get_logger
from app.llm.gateway import LLMGatewayError, call
from app.llm.providers import LLMProvider
from app.orchestrator.agents.coordinator.plan import (
    COORDINATOR_WRITABLE,
    EditOp,
    PlanEdit,
    replan,
)
from app.orchestrator.progress.hashing import progress_hash
from app.orchestrator.prompts import render
from app.schemas.constraints import ConstraintStrength
from app.schemas.coordinator import (
    ALLOWED_ACTIONS,
    Action,
    ConstraintView,
    CoordinatorDecision,
    CoordinatorEvent,
    CoordinatorInput,
    CoordinatorPolicy,
    CoordinatorProgress,
    ProposalView,
    StepView,
    StopReason,
    Trigger,
)
from app.schemas.plan import Plan, StepStatus
from app.schemas.state import TaskState

logger = get_logger(__name__)

__all__ = ["COORDINATOR_WRITABLE", "CoordinatorResult", "build_input", "check_decision", "coordinate"]

_SITUATIONS = {
    Trigger.NONE_VIABLE: "No candidate satisfies the user's requirements. Decide whether to widen or ask the user.",
    Trigger.DEADLINE_AT_RISK: "The Monitor reports the delivery deadline is at risk. Decide how to recover.",
}
# Steps re-run after each action: from this step id onward, in plan order.
_RESET_FROM = {Action.WIDEN: "discover", Action.REPLAN: "discover", Action.SWITCH_CANDIDATE: "negotiate"}


@dataclass(frozen=True, slots=True)
class CoordinatorResult:
    """What the Router receives. ``plan_edits`` are already applied in the returned state update."""

    action: Action
    reason: str
    proposal_id: str | None = None
    candidate_id: str | None = None
    plan_edits: tuple[PlanEdit, ...] = ()
    fallback: bool = False
    stop_reason: StopReason | None = None


class _Rejected(Exception):
    """Model output failed a deterministic check; the message is fed back for one retry."""


# --------------------------------------------------------------------------- input


def _plan_of(state: TaskState) -> Plan | None:
    raw = state.get("plan")
    return Plan.model_validate(raw) if raw else None


def build_input(state: TaskState, event: CoordinatorEvent, progress: CoordinatorProgress) -> CoordinatorInput:
    """Project the task state to ids, field names and counts. No candidate or user free text."""
    constraints = state.get("constraints", [])
    strength_by_id = {c.constraint_id: c.strength for c in constraints}
    plan = _plan_of(state)
    return CoordinatorInput(
        trigger=event.trigger,
        allowed_actions=tuple(sorted(ALLOWED_ACTIONS[event.trigger], key=lambda a: a.value)),
        iteration=progress.iterations + 1,
        constraints=tuple(
            ConstraintView(
                constraint_id=c.constraint_id, field_name=c.field_name, operator=c.operator.value, strength=c.strength
            )
            for c in constraints
            if c.active
        ),
        proposals=tuple(
            ProposalView(
                proposal_id=p.proposal_id,
                field_name=p.field_name,
                # An unknown constraint is treated as hard: it can never be relaxed without the user.
                strength=strength_by_id.get(p.constraint_id, ConstraintStrength.HARD),
                unlocked_count=len(p.unlocked_ids),
                cost=p.cost,
            )
            for p in state.get("relaxation_proposals", [])
        ),
        alternatives=event.alternatives,
        can_widen_search=event.can_widen_search,
        payment_committed=event.payment_committed,
        days_late=event.days_late,
        plan=tuple(StepView(step_id=s.step_id, status=s.status.value) for s in plan.steps) if plan else (),
    )


# --------------------------------------------------------------------------- validation


def check_decision(decision: CoordinatorDecision, context: CoordinatorInput) -> None:
    """Raise ``_Rejected`` unless the choice is allowed for the trigger and its ids are real."""
    if decision.action not in context.allowed_actions:
        allowed = ", ".join(a.value for a in context.allowed_actions)
        raise _Rejected(f"action {decision.action.value!r} is not allowed here; choose one of: {allowed}")
    if decision.action is Action.WIDEN:
        if decision.proposal_id is not None:
            proposal = next((p for p in context.proposals if p.proposal_id == decision.proposal_id), None)
            if proposal is None:
                raise _Rejected(f"unknown proposal_id {decision.proposal_id!r}")
            if proposal.strength is not ConstraintStrength.SOFT:
                raise _Rejected("a hard constraint cannot be relaxed without the user; choose ask_user")
        elif not context.can_widen_search:
            raise _Rejected("search cannot be widened and no soft proposal was given; choose ask_user")
    elif decision.action is Action.SWITCH_CANDIDATE and decision.candidate_id not in context.alternatives:
        raise _Rejected(f"candidate_id {decision.candidate_id!r} is not one of the alternatives")


async def _decide(
    context: CoordinatorInput, policy: CoordinatorPolicy, provider: LLMProvider | None
) -> tuple[CoordinatorDecision | None, str]:
    """Ask the model, retrying on a parse or validation failure. ``(None, why)`` means fall back."""
    prompt = render("coordinator", situation=_SITUATIONS[context.trigger], state=context.model_dump_json(indent=2))
    current, why = prompt, ""
    for _ in range(policy.decision_attempts):
        try:
            decision = await call(current, CoordinatorDecision, retries=policy.parse_retries, provider=provider)
        except LLMGatewayError as exc:
            logger.warning("coordinator_llm_failed", extra={"error": str(exc)})
            return None, "model output unavailable or unparseable"
        try:
            check_decision(decision, context)
        except _Rejected as exc:
            why = str(exc)
            logger.warning("coordinator_output_rejected", extra={"reason": why})
            current = f"{prompt}\n\nYour previous answer was rejected: {why}. Answer again and fix this."
            continue
        return decision, ""
    return None, f"model output rejected after retry: {why}"


# --------------------------------------------------------------------------- plan edits


def build_plan_edits(plan: Plan, action: Action) -> list[PlanEdit]:
    """Re-queue the steps that must run again: every step from the action's restart point onward."""
    first = _RESET_FROM.get(action)
    ids = [s.step_id for s in plan.steps]
    if first is None or first not in ids:
        return []
    return [
        PlanEdit(op=EditOp.SET_STATUS, step_id=s.step_id, status=StepStatus.PENDING)
        for s in plan.steps[ids.index(first) :]
        if s.status is not StepStatus.PENDING
    ]


# --------------------------------------------------------------------------- entry point


def _stop_reason(progress: CoordinatorProgress, key: str, policy: CoordinatorPolicy) -> StopReason | None:
    if progress.iterations >= policy.max_iterations:
        return StopReason.ITERATION_CAP
    if key in progress.seen_hashes:
        return StopReason.NO_PROGRESS
    return None


async def coordinate(
    state: TaskState,
    event: CoordinatorEvent,
    *,
    audit: AuditScope,
    policy: CoordinatorPolicy | None = None,
    provider: LLMProvider | None = None,
) -> tuple[CoordinatorResult, dict[str, object]]:
    """Decide, apply the plan edits, and return ``(result, partial TaskState update)``.

    The update is limited to ``COORDINATOR_WRITABLE``. Never raises for bad model output.
    """
    policy = policy or CoordinatorPolicy()
    progress = state.get("coordinator") or CoordinatorProgress()
    digest = progress_hash((c.candidate_id for c in state.get("candidates", [])), state.get("constraints", []))
    key = f"{event.trigger.value}:{digest}"

    stop = _stop_reason(progress, key, policy)
    if stop is not None:
        stopped = CoordinatorResult(Action.STOP, f"coordinator stopped: {stop.value}", stop_reason=stop)
        audit.emit(
            Phase.PLAN, "COORDINATOR_STOPPED", node="coordinator", decision=Action.STOP.value, reason=stopped.reason,
            metadata={"trigger": event.trigger.value, "iterations": progress.iterations},
        )
        return stopped, {"coordinator": progress.model_copy(update={"last_action": Action.STOP, "stop_reason": stop})}

    context = build_input(state, event, progress)
    decision, why = await _decide(context, policy, provider)
    if decision is None:
        result = CoordinatorResult(Action.ASK_USER, f"fallback to ask user: {why}", fallback=True)
        audit.emit(
            Phase.PLAN, "COORDINATOR_FALLBACK", node="coordinator", decision=Action.ASK_USER.value, reason=why,
            metadata={"trigger": event.trigger.value},
        )
    else:
        result = CoordinatorResult(
            decision.action,
            decision.reason,
            proposal_id=decision.proposal_id if decision.action is Action.WIDEN else None,
            candidate_id=decision.candidate_id if decision.action is Action.SWITCH_CANDIDATE else None,
        )

    update: dict[str, object] = {
        "coordinator": progress.model_copy(
            update={
                "iterations": progress.iterations + 1,
                "seen_hashes": (*progress.seen_hashes, key),
                "last_action": result.action,
            }
        )
    }
    plan = _plan_of(state)
    edits = build_plan_edits(plan, result.action) if plan else []
    if plan and edits:
        update |= replan(plan, edits, run_id=audit.run_id, reason=f"{result.action.value}: {result.reason}", audit=audit.sink)
        result = replace(result, plan_edits=tuple(edits))

    audit.emit(
        Phase.PLAN, "COORDINATOR_DECIDED", node="coordinator", actor_type="agent", decision=result.action.value,
        reason=result.reason, payload=context.model_dump(mode="json"),
        metadata={
            "trigger": event.trigger.value, "fallback": result.fallback, "proposal_id": result.proposal_id,
            "candidate_id": result.candidate_id, "plan_edits": len(result.plan_edits),
        },
    )
    return result, update
