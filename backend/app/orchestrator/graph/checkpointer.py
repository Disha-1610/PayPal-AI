"""LangGraph checkpoint persistence.

PostgresSaver (production) / SqliteSaver (development). LangGraph owns the checkpoint tables
(``checkpoints``, ``checkpoint_blobs``, ``checkpoint_writes``) and creates them via ``setup()``;
we intentionally do not model or migrate them with Alembic.

Checkpoints are deserialised with an explicit allow-list (``checkpoint_serde``): only our own schema types may be
rebuilt from stored bytes, so a tampered checkpoint cannot instantiate arbitrary classes. LangGraph will make this
mandatory in a future release.
"""

import importlib
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

import app.schemas
from app.core.config import Settings, settings


def schema_types() -> tuple[tuple[str, str], ...]:
    """Every pydantic model and enum defined in ``app.schemas``: the types task state is made of."""
    found: list[tuple[str, str]] = []
    for info in pkgutil.iter_modules(app.schemas.__path__, f"{app.schemas.__name__}."):
        module = importlib.import_module(info.name)
        for name, value in vars(module).items():
            if isinstance(value, type) and value.__module__ == module.__name__ and issubclass(value, (BaseModel, Enum)):
                found.append((module.__name__, name))
    return tuple(sorted(found))


def checkpoint_serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=list(schema_types()))


@asynccontextmanager
async def open_checkpointer(cfg: Settings = settings) -> AsyncIterator[BaseCheckpointSaver]:
    """Yield a ready-to-use async checkpointer chosen by ``DATABASE_URL``."""
    serde = checkpoint_serde()
    if cfg.is_sqlite:
        async with aiosqlite.connect(cfg.checkpoint_sqlite_path) as conn:
            saver = AsyncSqliteSaver(conn, serde=serde)
            await saver.setup()
            yield saver
    else:
        # psycopg expects a plain libpq URL (no SQLAlchemy driver suffix).
        conninfo = cfg.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        async with AsyncPostgresSaver.from_conn_string(conninfo, serde=serde) as saver:
            await saver.setup()
            yield saver
