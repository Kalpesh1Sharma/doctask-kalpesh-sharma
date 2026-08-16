from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenAIModelPricing(BaseModel):
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal

    @field_validator("input_cost_per_million", "output_cost_per_million")
    @classmethod
    def require_non_negative_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("OpenAI model pricing must be non-negative")
        return value


class Settings(BaseSettings):
    app_name: str = "task1"
    environment: str = "development"

    database_url: str

    database_pool_min_size: int = 2
    database_pool_max_size: int = 10

    artifact_root: Path = Path("var/artifacts")
    source_root: Path = Path("var/sources")

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_timeout_seconds: float = 60.0
    openai_fact_extraction_model: str = "gpt-4.1-mini"
    openai_contradiction_model: str = "gpt-4.1-mini"
    openai_rule_evaluation_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_model_pricing: dict[str, OpenAIModelPricing] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
