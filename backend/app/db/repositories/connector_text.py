from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import TextRecord
from app.db.models import ConnectorText


class SqlTextStore:
    """TextStore backed by the ``connector_texts`` table (one transaction per save)."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def save(self, *, run_id: str, source: str, subject_id: str, records: list[TextRecord]) -> list[str]:
        rows = [
            ConnectorText(
                run_id=run_id,
                source=source,
                subject_id=subject_id,
                kind=r.kind,
                body=r.body,
                rating=r.rating,
                posted_at=r.posted_at,
            )
            for r in records
        ]
        async with self._factory() as session:
            async with session.begin():
                session.add_all(rows)
        return [row.id.hex for row in rows]
