from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelPricing(BaseModel):
    input_cost_per_million: Decimal
    output_cost_per_million: Decimal

    @field_validator("input_cost_per_million", "output_cost_per_million")
    @classmethod
    def require_non_negative_cost(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("model pricing must be non-negative")
        return value


OpenAIModelPricing = ModelPricing
GroqModelPricing = ModelPricing


class Settings(BaseSettings):
    app_name: str = "task1"
    environment: str = "development"

    database_url: str

    database_pool_min_size: int = 2
    database_pool_max_size: int = 10

    artifact_root: Path = Path("var/artifacts")
    source_root: Path = Path("var/sources")

    # Production semantic workflow provider. Groq's GPT-OSS 20B supports
    # JSON-schema structured outputs and is available on Groq's free plan.
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com"
    groq_timeout_seconds: float = 60.0
    groq_fact_extraction_model: str = "openai/gpt-oss-20b"
    groq_contradiction_model: str = "openai/gpt-oss-20b"
    groq_rule_evaluation_model: str = "openai/gpt-oss-20b"
    groq_document_classification_model: str = "openai/gpt-oss-20b"
    groq_model_pricing: dict[str, GroqModelPricing] = Field(default_factory=dict)

    # Conservative process-local pacing. The published free-plan ceiling for
    # GPT-OSS 20B is currently 30 RPM; the default stays materially below it.
    groq_max_requests_per_minute: int = 20
    groq_min_request_interval_seconds: float = 3.1
    groq_rate_limit_reserve_tokens: int = 512
    groq_max_completion_tokens: int = 1024
    groq_max_retries: int = 4
    groq_backoff_base_seconds: float = 1.0
    groq_backoff_max_seconds: float = 30.0
    groq_estimated_chars_per_token: float = 3.5

    document_classification_max_chunks: int = 8
    document_classification_max_chars_per_chunk: int = 1500
    analysis_retrieval_top_k: int = 12
    analysis_retrieval_query: str = (
        "Material mortgage-document facts: parties, obligations, amounts, dates, terms, "
        "approvals, disclosures, settlement details, and compliance-relevant statements."
    )

    watch_poll_interval_seconds: float = 1.0
    watch_stable_after_seconds: float = 1.0
    watch_retry_delay_seconds: float = 5.0
    watch_pile_limit: int = 200

    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 100
    worker_max_advances_per_run: int = 8

    # Legacy OpenAI adapter configuration remains available for backwards
    # compatibility/tests, but production external wiring no longer requires it.
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    openai_timeout_seconds: float = 60.0
    openai_fact_extraction_model: str = "gpt-4.1-mini"
    openai_contradiction_model: str = "gpt-4.1-mini"
    openai_rule_evaluation_model: str = "gpt-4.1-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_model_pricing: dict[str, OpenAIModelPricing] = Field(default_factory=dict)

    @field_validator(
        "groq_max_requests_per_minute",
        "groq_max_completion_tokens",
    )
    @classmethod
    def require_positive_groq_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Groq rate/output limits must be positive")
        return value

    @field_validator("groq_rate_limit_reserve_tokens", "groq_max_retries")
    @classmethod
    def require_non_negative_groq_integer(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Groq reserve/retry values must be non-negative")
        return value

    @field_validator(
        "document_classification_max_chunks",
        "document_classification_max_chars_per_chunk",
        "analysis_retrieval_top_k",
        "watch_pile_limit",
        "worker_batch_size",
        "worker_max_advances_per_run",
    )
    @classmethod
    def require_positive_runtime_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("runtime limits must be positive")
        return value

    @field_validator(
        "groq_min_request_interval_seconds",
        "groq_backoff_base_seconds",
        "groq_backoff_max_seconds",
        "groq_estimated_chars_per_token",
    )
    @classmethod
    def require_positive_groq_number(cls, value: float, info) -> float:
        if info.field_name == "groq_min_request_interval_seconds":
            if value < 0:
                raise ValueError("Groq minimum request interval must be non-negative")
        elif value <= 0:
            raise ValueError("Groq backoff/token-estimate values must be positive")
        return value

    @field_validator(
        "watch_poll_interval_seconds",
        "watch_stable_after_seconds",
        "watch_retry_delay_seconds",
        "worker_poll_interval_seconds",
    )
    @classmethod
    def require_positive_runtime_number(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("watch runtime intervals must be positive")
        return value

    @field_validator("analysis_retrieval_query")
    @classmethod
    def require_nonempty_retrieval_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("analysis retrieval query must not be empty")
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
