from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


def create_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(url or settings.async_database_url, pool_pre_ping=True)


engine = create_engine()
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: async_sessionmaker[AsyncSession] = SessionFactory) -> AsyncIterator[AsyncSession]:
    """One transaction: commit on success, roll back on any error."""
    async with factory() as session:
        async with session.begin():
            yield session
