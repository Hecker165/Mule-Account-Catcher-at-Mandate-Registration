"""Application settings using pydantic-settings.

A0 owns this module. Credential fields use SecretStr and are never serialised.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application configuration.

    Reads from environment variables and ``.env`` file. Unknown fields are
    silently ignored so that services can share the same ``.env`` safely.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────
    app_env: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── Backing services (A1/A4 connect; A0 only declares) ───────────────
    postgres_url: str = (
        "postgresql+asyncpg://mandate_guardian:mandate_guardian@localhost:5432/mandate_guardian"
    )
    redis_url: str = "redis://localhost:6379/0"

    # ── Security ─────────────────────────────────────────────────────────
    hmac_pepper: SecretStr = SecretStr("replace-with-a-local-development-secret")

    # ── Razorpay (A3/A6 — not used in A0) ────────────────────────────────
    razorpay_key_id: SecretStr | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None

    # ── LiteLLM (A12 — optional, not used in A0) ─────────────────────────
    litellm_base_url: str | None = None
    litellm_api_key: SecretStr | None = None
    litellm_model: str | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
