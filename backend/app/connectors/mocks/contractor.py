from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.connectors.base import Capability, specs
from app.connectors.base.models import Listing, ReviewText
from app.connectors.mocks.fixture import FixtureConnector
from app.schemas.candidate import Candidate

_NOW = datetime.now(timezone.utc)


def build_mock_contractor() -> FixtureConnector:
    """A non-shopping source: same interface and Candidate, different domain plug-in."""
    return FixtureConnector(
        id="mock_contractors",
        domain="contractor",
        capabilities=specs(Capability.SEARCH, Capability.DETAILS, Capability.REVIEWS, Capability.CHECKOUT),
        listings=(
            Listing(
                candidate=Candidate(
                    candidate_id="ct-1",
                    source="mock_contractors",
                    domain="contractor",
                    title="Acme web developer",
                    total_cost=Decimal("60000.00"),
                    currency="INR",
                    rating=4.8,
                    review_count=57,
                    latest_review_at=_NOW - timedelta(days=5),
                    available_by=_NOW + timedelta(days=7),
                    attributes={"skills": ["python", "react"], "location": "remote"},
                ),
                description="Full-stack developer, fixed-price projects.",
            ),
        ),
        reviews={"ct-1": (ReviewText(rating=5, text="Delivered on time.", posted_at=_NOW - timedelta(days=5)),)},
    )
