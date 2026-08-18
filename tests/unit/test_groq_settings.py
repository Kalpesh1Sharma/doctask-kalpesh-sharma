import pytest
from pydantic import ValidationError

from config.settings import Settings


def test_groq_defaults_are_conservative_and_use_gpt_oss_20b() -> None:
    settings = Settings(database_url="postgresql://example", _env_file=None)

    assert settings.groq_api_key is None
    assert settings.groq_base_url == "https://api.groq.com"
    assert settings.groq_fact_extraction_model == "openai/gpt-oss-20b"
    assert settings.groq_contradiction_model == "openai/gpt-oss-20b"
    assert settings.groq_rule_evaluation_model == "openai/gpt-oss-20b"
    assert settings.groq_document_classification_model == "openai/gpt-oss-20b"
    assert settings.groq_max_requests_per_minute == 20
    assert settings.groq_min_request_interval_seconds >= 3.0
    assert settings.groq_rate_limit_reserve_tokens == 512
    assert settings.groq_max_completion_tokens == 1024
    assert settings.document_classification_max_chunks == 8
    assert settings.watch_stable_after_seconds == 1.0
    assert settings.worker_poll_interval_seconds == 1.0
    assert settings.worker_batch_size == 100
    assert settings.worker_max_advances_per_run == 8


def test_invalid_groq_pacing_configuration_fails_fast() -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql://example",
            groq_max_requests_per_minute=0,
            _env_file=None,
        )


def test_analysis_retrieval_settings_are_positive_and_nonempty(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/task1")
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_TOP_K", "7")
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_QUERY", "material payment obligations")

    settings = Settings(_env_file=None)

    assert settings.analysis_retrieval_top_k == 7
    assert settings.analysis_retrieval_query == "material payment obligations"


def test_analysis_retrieval_rejects_invalid_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/task1")
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_TOP_K", "0")
    with pytest.raises(ValueError, match="runtime limits"):
        Settings(_env_file=None)

    monkeypatch.setenv("ANALYSIS_RETRIEVAL_TOP_K", "12")
    monkeypatch.setenv("ANALYSIS_RETRIEVAL_QUERY", "   ")
    with pytest.raises(ValueError, match="retrieval query"):
        Settings(_env_file=None)


def test_worker_settings_reject_non_positive_values() -> None:
    with pytest.raises(ValueError, match="runtime limits"):
        Settings(
            database_url="postgresql://example",
            worker_batch_size=0,
            _env_file=None,
        )
    with pytest.raises(ValueError, match="runtime intervals"):
        Settings(
            database_url="postgresql://example",
            worker_poll_interval_seconds=0,
            _env_file=None,
        )
