from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "task1"
    environment: str = "development"

    database_url: str = (
        "postgresql://task1:task1_dev_password@localhost:5432/task1"
    )

    database_pool_min_size: int = 2
    database_pool_max_size: int = 10

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()