"""Runtime configuration, loaded from environment variables and `.env`."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    anthropic_api_key: str | None = None
    analyst_model: str = "claude-sonnet-5-5"
    refusal_fallback: bool = True

    # MongoDB Atlas
    mongodb_uri: str | None = None
    mongodb_db: str = "recursive_analyst"

    # Rounds
    max_rounds: int = Field(default=5, ge=1)
    max_usd: float = Field(default=25.0, gt=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
