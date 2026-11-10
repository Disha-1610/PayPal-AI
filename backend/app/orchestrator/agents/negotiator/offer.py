"""Sanitize a raw counter-offer (contractor / A2A payload) into the strict structured model.

Only known fields survive; every other key, including any prose, is dropped. Terms must be machine tokens
(see ``CounterOffer``), so free text cannot be smuggled through a field either. Invalid data raises
``ValidationError`` and the offer is rejected as a whole.
"""

from collections.abc import Mapping

from app.schemas.negotiator import CounterMilestone, CounterOffer


def _pick(raw: Mapping[str, object], model: type[CounterOffer] | type[CounterMilestone]) -> dict[str, object]:
    return {name: raw[name] for name in model.model_fields if name in raw}


def parse_counter_offer(raw: Mapping[str, object]) -> CounterOffer:
    fields = _pick(raw, CounterOffer)
    milestones = fields.get("milestones")
    if isinstance(milestones, list | tuple):
        fields["milestones"] = [_pick(m, CounterMilestone) if isinstance(m, Mapping) else m for m in milestones]
    return CounterOffer.model_validate(fields)
