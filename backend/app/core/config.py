from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "PayPal-AI"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/paypal_ai"
    checkpoint_sqlite_path: str = "checkpoints.sqlite"

    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash"
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 30.0

    approval_signing_key: SecretStr | None = None  # HMAC key for user approvals; required to issue/verify
    approval_key_id: str = "v1"  # recorded on each approval so the key can be rotated
    approval_ttl_seconds: int = 600

    paypal_client_id: str | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"  # live: https://api-m.paypal.com
    paypal_timeout_seconds: float = 15.0
    paypal_webhook_id: str | None = None  # webhook registered in the PayPal app; needed to verify signatures

    channel3_api_key: SecretStr | None = None
    channel3_base_url: str = "https://api.trychannel3.com"
    channel3_country: str | None = None  # e.g. "US"; Channel3 infers defaults when unset
    channel3_currency: str | None = None  # e.g. "USD"
    channel3_timeout_seconds: float = 10.0

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
