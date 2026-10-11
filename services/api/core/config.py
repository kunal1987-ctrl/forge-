"""Global application configuration management using pydantic-settings.

Provides secure retrieval of API keys and environment settings from .env file
with validation and type safety.
"""

from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve absolute path to .env in project root regardless of CWD
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """Central settings model for FORGE application secrets and configuration."""

    OPENAI_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    GITHUB_TOKEN: str = ""
    DATABASE_URL: str = "postgresql+asyncpg://forge:forge_password@localhost:5432/forge_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    model_config = SettingsConfigDict(
        env_file=str(_ENV_PATH) if _ENV_PATH.exists() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
