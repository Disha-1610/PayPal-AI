from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PayPal-AI"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/paypal_ai"
    checkpoint_sqlite_path: str = "checkpoints.sqlite"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def async_database_url(self) -> str:
        """Driver-qualified URL for the async application engine."""
        return _with_driver(self.database_url, postgres="postgresql+asyncpg", sqlite="sqlite+aiosqlite")

    @property
    def sync_database_url(self) -> str:
        """Driver-qualified URL for Alembic (sync engine)."""
        return _with_driver(self.database_url, postgres="postgresql+psycopg", sqlite="sqlite")


def _with_driver(url: str, *, postgres: str, sqlite: str) -> str:
    scheme, sep, rest = url.partition("://")
    base = scheme.split("+", 1)[0]
    if base in ("postgresql", "postgres"):
        return f"{postgres}{sep}{rest}"
    if base == "sqlite":
        return f"{sqlite}{sep}{rest}"
    raise ValueError(f"Unsupported database scheme: {scheme!r}")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
