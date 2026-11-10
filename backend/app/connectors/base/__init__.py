from app.connectors.base.connector import (
    STANDARD_SPECS,
    Capability,
    CapabilitySpec,
    Connector,
    ConnectorError,
    TextRecord,
    TextStore,
    specs,
)

# Request models carry no untrusted text, so LLM-facing code may import them from here. Response
# models (``Listing``, ``ReviewText``) stay in ``connectors.base.models`` and out of the orchestrator.
from app.connectors.base.models import (
    CandidateRequest,
    DeliveryRequest,
    ReviewsRequest,
    SearchRequest,
)

__all__ = [
    "STANDARD_SPECS",
    "CandidateRequest",
    "Capability",
    "CapabilitySpec",
    "Connector",
    "ConnectorError",
    "DeliveryRequest",
    "ReviewsRequest",
    "SearchRequest",
    "TextRecord",
    "TextStore",
    "specs",
]
