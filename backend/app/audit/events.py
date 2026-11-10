"""One way to build and emit lifecycle audit events.

Every event carries the stable shape ``{task_id (run_id), timestamp, phase, event_type (action),
input_hash, decision, reason}``; ``event_id`` and ``timestamp`` are assigned by the audit row.
Money, approval and Guard decisions use ``emit_critical`` (same transaction as the action, so the
action cannot commit without its record); everything else uses ``emit`` (background writer).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import AuditEvent, hash_input, record_critical

_REASON_MAX = 512


class Phase(StrEnum):
    INTENT = "intent"
    PLAN = "plan"
    DISCOVERY = "discovery"
    EVALUATION = "evaluation"
    ADAPT = "adapt"
    EXECUTION = "execution"  # approval, authorization, verification, capture / void
    MONITOR = "monitor"
    DOMAIN = "domain"  # domain plug-in lifecycle (e.g. contractor milestones)


class AuditSink(Protocol):
    def enqueue(self, event: AuditEvent) -> None: ...


def audit_event(
    run_id: str,
    phase: Phase,
    event_type: str,
    *,
    node: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    payload: object | None = None,
    decision: str | None = None,
    reason: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    state_diff: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Build an event. ``payload`` (JSON-serialisable) is stored only as its hash, never raw."""
    return AuditEvent(
        run_id=run_id, phase=phase.value, node=node, actor_type=actor_type, actor_id=actor_id, action=event_type,
        resource_type=resource_type, resource_id=resource_id,
        input_hash=None if payload is None else hash_input(payload),
        decision=decision, reason=reason[:_REASON_MAX] if reason else None, state_diff=state_diff, metadata_json=metadata,
    )


def emit(sink: AuditSink, run_id: str, phase: Phase, event_type: str, **fields: Any) -> None:
    """Queue a non-critical event on the background writer."""
    sink.enqueue(audit_event(run_id, phase, event_type, **fields))


@dataclass(frozen=True)
class AuditScope:
    """A sink bound to one run, so pipeline functions take a single optional ``audit`` argument."""

    sink: AuditSink
    run_id: str

    def emit(self, phase: Phase, event_type: str, **fields: Any) -> None:
        emit(self.sink, self.run_id, phase, event_type, **fields)


async def emit_critical(session: AsyncSession, run_id: str, phase: Phase, event_type: str, **fields: Any) -> None:
    """Write the event inside the caller's transaction; raises (rolling the action back) if it cannot."""
    await record_critical(session, audit_event(run_id, phase, event_type, **fields))
