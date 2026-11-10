"""Turn an external platform's event dict into a ``MonitorEvent``.

Seller, contractor and A2A connectors each map their own format to the common field set; this validates
the result so malformed or oversized input is rejected at the boundary instead of reaching the engine.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.monitor.events.deadline import InvalidEvent
from app.schemas.monitor import MonitorEvent

MAX_PAYLOAD_KEYS = 50


def parse_event(raw: Mapping[str, Any]) -> MonitorEvent:
    try:
        event = MonitorEvent.model_validate(dict(raw))
    except ValidationError as exc:
        # Field names only: the input may be attacker-controlled and is not echoed back.
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
        raise InvalidEvent(f"malformed event: invalid fields {fields}") from exc
    if len(event.payload) > MAX_PAYLOAD_KEYS:
        raise InvalidEvent("event payload has too many fields")
    return event
