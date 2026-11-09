"""LangGraph checkpoint persistence.

PostgresSaver (production) / SqliteSaver (development). LangGraph owns the checkpoint tables
(``checkpoints``, ``checkpoint_blobs``, ``checkpoint_writes``) and creates them via ``setup()``;
we intentionally do not model or migrate them with Alembic.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import Settings, settings


@asynccontextmanager
async def open_checkpointer(cfg: Settings = settings) -> AsyncIterator[BaseCheckpointSaver]:
    """Yield a ready-to-use async checkpointer chosen by ``DATABASE_URL``."""
    if cfg.is_sqlite:
        async with AsyncSqliteSaver.from_conn_string(cfg.checkpoint_sqlite_path) as saver:
            await saver.setup()
            yield saver
    else:
        # psycopg expects a plain libpq URL (no SQLAlchemy driver suffix).
        conninfo = cfg.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
            await saver.setup()
            yield saver
