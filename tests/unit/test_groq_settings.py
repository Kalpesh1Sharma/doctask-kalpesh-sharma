import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_groq_defaults_are_conservative_and_use_gpt_oss_20b() -> None:
    settings = Settings(database_url="postgresql://example", _env_file=None)

    assert settings.groq_api_key is None
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"
    assert settings.groq_fact_extraction_model == "openai/gpt-oss-20b"
    assert settings.groq_contradiction_model == "openai/gpt-oss-20b"
    assert settings.groq_rule_evaluation_model == "openai/gpt-oss-20b"
    assert settings.groq_max_requests_per_minute == 20
    assert settings.groq_min_request_interval_seconds >= 3.0
    assert settings.groq_rate_limit_reserve_tokens == 512
    assert settings.groq_max_completion_tokens == 1024


def test_invalid_groq_pacing_configuration_fails_fast() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://example",
            groq_max_requests_per_minute=0,
            _env_file=None,
        )
