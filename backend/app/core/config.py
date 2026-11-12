from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Aisling"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    database_url: str = "postgresql://postgres:postgres@localhost:5432/aisling"
    checkpoint_sqlite_path: str = "checkpoints.sqlite"

    llm_provider: str = "gemini"  # gemini | groq | cerebras | openrouter | openai_compat
    llm_model: str = "gemini-3.5-flash"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None  # openai_compat only; groq/cerebras/openrouter have built-in defaults
    llm_timeout_seconds: float = 12.0  # per attempt; structured extraction/scoring answers are short
    llm_thinking_budget: int = 0  # Gemini 2.5 thinking tokens; 0 disables thinking (big latency win), -1 = model decides
    llm_temperature: float = 0.0
    run_llm_budget_seconds: float = 90.0  # LLM time per graph invocation; once spent, agents use their code-only path (0 = unlimited)

    approval_signing_key: SecretStr | None = None  # HMAC key for user approvals; required to issue/verify
    approval_key_id: str = "v1"  # recorded on each approval so the key can be rotated
    approval_ttl_seconds: int = 600

    # Browser access (CORS) and user authentication.
    cors_origins: str = "http://localhost:3000"  # comma-separated exact origins; credentials are allowed for these only
    session_signing_key: SecretStr | None = None  # HMAC key for the sandbox session cookie (>= 32 chars)
    session_ttl_seconds: int = 8 * 3600
    auth_sandbox_enabled: bool = False  # anonymous sandbox sign-in; honoured only when environment == "development"
    auth_jwt_issuer: str | None = None  # external identity provider: all three are required to enable JWT sign-in
    auth_jwt_audience: str | None = None
    auth_jwks_url: str | None = None

    paypal_client_id: str | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"  # live: https://api-m.paypal.com
    paypal_timeout_seconds: float = 15.0
    paypal_webhook_id: str | None = None  # webhook registered in the PayPal app; needed to verify signatures
    paypal_payee_merchant_id: str | None = None  # merchant account that receives shopping payments (sandbox business account)

    channel3_api_key: SecretStr | None = None
    channel3_base_url: str = "https://api.trychannel3.com"
    channel3_country: str | None = None  # e.g. "US"; Channel3 infers defaults when unset
    channel3_currency: str | None = None  # e.g. "USD"
    channel3_timeout_seconds: float = 10.0

    # In-app sandbox contractors and remote agents, so contractor and A2A runs have a counterparty (like PayPal sandbox).
    sandbox_counterparties_enabled: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sandbox_auth_active(self) -> bool:
        return self.auth_sandbox_enabled and self.environment == "development"

    @property
    def jwt_auth_active(self) -> bool:
        return bool(self.auth_jwt_issuer and self.auth_jwt_audience and self.auth_jwks_url)

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
