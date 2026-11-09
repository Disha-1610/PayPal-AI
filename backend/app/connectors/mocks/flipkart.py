from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.connectors.base import Capability, specs
from app.connectors.base.models import Listing, ReviewText
from app.connectors.mocks.fixture import FixtureConnector
from app.schemas.candidate import Candidate

_NOW = datetime.now(timezone.utc)


def build_mock_flipkart() -> FixtureConnector:
    """Read-only platform: no stock, delivery or checkout (proves capability flags)."""
    return FixtureConnector(
        id="mock_flipkart",
        domain="shopping",
        capabilities=specs(Capability.SEARCH, Capability.DETAILS, Capability.REVIEWS),
        listings=(
            Listing(
                candidate=Candidate(
                    candidate_id="fk-1",
                    source="mock_flipkart",
                    domain="shopping",
                    title="SprintX running shoes",
                    total_cost=Decimal("3999.00"),
                    currency="INR",
                    rating=4.1,
                    review_count=240,
                    latest_review_at=_NOW - timedelta(days=40),
                    return_window_days=10,
                    attributes={"brand": "SprintX", "color": "black", "size": "9"},
                ),
                description="Everyday running shoes.",
            ),
        ),
        reviews={"fk-1": (ReviewText(rating=4, text="Good value for money.", posted_at=_NOW - timedelta(days=40)),)},
    )
