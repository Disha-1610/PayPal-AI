"""Completion check. A final DELIVERED/COMPLETED event is never enough on its own.

All of these must hold, and each failure is reported as a blocker:
  * every tracked entity's latest applied event is one of the domain's completed event types;
  * no entity's latest event leaves an open problem;
  * no payment is still open (created/authorized);
  * payment is settled, derived from the ledger rather than copied from an external event.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from app.domains.base import MonitorSpec

PAYMENT_ENTITY_PREFIX = "payment:"


@dataclass(frozen=True)
class EntityProgress:
    event_type: str
    problem: str | None


@dataclass(frozen=True)
class PaymentSummary:
    settled: bool  # at least one captured payment
    open_count: int  # payments still created/authorized


@dataclass(frozen=True)
class Completion:
    done: bool
    blockers: tuple[str, ...]


def check_completion(
    spec: MonitorSpec,
    latest: Mapping[str, EntityProgress],
    payments: PaymentSummary,
    expected_entities: frozenset[str] = frozenset(),
) -> Completion:
    work = {k: v for k, v in latest.items() if not k.startswith(PAYMENT_ENTITY_PREFIX)}
    blockers: list[str] = []
    entities = expected_entities or frozenset(work)
    if not entities:
        blockers.append("no progress reported yet")
    for entity in sorted(entities):
        progress = work.get(entity)
        if progress is None:
            blockers.append(f"{entity}: no events received")
        elif progress.problem is not None:
            blockers.append(f"{entity}: open problem {progress.problem}")
        elif progress.event_type not in spec.completed_event_types:
            blockers.append(f"{entity}: not complete (latest {progress.event_type})")
    if payments.open_count:
        blockers.append(f"{payments.open_count} payment(s) still open")
    if not payments.settled:
        blockers.append("payment not settled")
    return Completion(done=not blockers, blockers=tuple(blockers))
