from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.application import provider_usage_tracking
from api.application.provider_usage_tracking import finish_provider_usage, start_provider_usage
from config.settings import GroqModelPricing, OpenAIModelPricing
from db.pool import pool
from domain.run import RunStage, RunStepStatus
from repositories.provider_usage_repository import ProviderUsageRepository, ProviderUsageState
from repositories.run_step_repository import RunStepRepository
from services.openai_structured_transport import OpenAIStructuredTransport, ProviderUsage
from services.openai_task_providers import OpenAIFactExtractionProvider


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def _running_understand_step() -> str:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"provider-usage-tracking-{uuid4()}",),
        ).fetchone()
        assert pile is not None
        run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s, 'MANUAL'::run_type, 'RUNNING'::run_status)
            RETURNING id
            """,
            (pile[0],),
        ).fetchone()
        assert run is not None
        step = RunStepRepository(connection).create(str(run[0]), RunStage.UNDERSTAND, 1)
        running = RunStepRepository(connection).update_execution_state(
            step.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
        )
        assert running is not None
        connection.commit()
        return str(run[0])


def test_openai_provider_identity_is_durable_before_the_call() -> None:
    run_id = _running_understand_step()
    provider = OpenAIFactExtractionProvider(OpenAIStructuredTransport("key"), "gpt-usage-model")

    receipt = start_provider_usage(
        pool,
        run_id=run_id,
        stage=RunStage.UNDERSTAND,
        operation_scope="fact-extraction-v1",
        operation_key=f"understand:{run_id}",
        claim_generation=1,
        provider=provider,
    )

    assert receipt is not None
    with pool.connection() as connection:
        events = ProviderUsageRepository(connection).list_for_step(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert len(events) == 1
    assert events[0].provider_name == "openai"
    assert events[0].model_name == "gpt-usage-model"
    assert events[0].state is ProviderUsageState.STARTED


def test_real_usage_model_and_configured_decimal_price_are_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _running_understand_step()
    provider = SimpleNamespace(provider_name="openai")
    receipt = start_provider_usage(
        pool,
        run_id=run_id,
        stage=RunStage.UNDERSTAND,
        operation_scope="fact-extraction-v1",
        operation_key=f"understand:{run_id}",
        claim_generation=1,
        provider=provider,
    )
    assert receipt is not None
    monkeypatch.setattr(
        provider_usage_tracking,
        "get_settings",
        lambda: SimpleNamespace(
            openai_model_pricing={
                "gpt-priced": OpenAIModelPricing(
                    input_cost_per_million=Decimal("0.50"),
                    output_cost_per_million=Decimal("1.25"),
                ),
            },
        ),
    )

    finish_provider_usage(
        pool,
        receipt,
        ProviderUsage("openai", "gpt-priced", 1_000_000, 2_000_000),
    )

    with pool.connection() as connection:
        event = ProviderUsageRepository(connection).list_for_step(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )[0]
    assert event.provider_name == "openai"
    assert event.model_name == "gpt-priced"
    assert event.input_tokens == 1_000_000
    assert event.output_tokens == 2_000_000
    assert event.estimated_cost == Decimal("3.00000000")


def test_missing_model_price_leaves_estimated_cost_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _running_understand_step()
    receipt = start_provider_usage(
        pool,
        run_id=run_id,
        stage=RunStage.UNDERSTAND,
        operation_scope="fact-extraction-v1",
        operation_key=f"understand:{run_id}",
        claim_generation=1,
        provider=SimpleNamespace(provider_name="openai", model_name="gpt-unpriced"),
    )
    assert receipt is not None
    monkeypatch.setattr(
        provider_usage_tracking,
        "get_settings",
        lambda: SimpleNamespace(openai_model_pricing={}),
    )

    finish_provider_usage(
        pool,
        receipt,
        ProviderUsage("openai", "gpt-unpriced", 10, 20),
    )

    with pool.connection() as connection:
        event = ProviderUsageRepository(connection).list_for_step(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )[0]
    assert event.model_name == "gpt-unpriced"
    assert event.estimated_cost is None


def test_groq_provider_identity_and_configured_decimal_price_are_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _running_understand_step()
    receipt = start_provider_usage(
        pool,
        run_id=run_id,
        stage=RunStage.UNDERSTAND,
        operation_scope="fact-extraction-v1",
        operation_key=f"understand:{run_id}",
        claim_generation=1,
        provider=SimpleNamespace(provider_name="groq", model_name="openai/gpt-oss-20b"),
    )
    assert receipt is not None
    monkeypatch.setattr(
        provider_usage_tracking,
        "get_settings",
        lambda: SimpleNamespace(
            groq_model_pricing={
                "openai/gpt-oss-20b": GroqModelPricing(
                    input_cost_per_million=Decimal("0.075"),
                    output_cost_per_million=Decimal("0.30"),
                ),
            },
            openai_model_pricing={},
        ),
    )

    finish_provider_usage(
        pool,
        receipt,
        ProviderUsage("groq", "openai/gpt-oss-20b", 1_000_000, 2_000_000),
    )

    with pool.connection() as connection:
        event = ProviderUsageRepository(connection).list_for_step(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )[0]
    assert event.provider_name == "groq"
    assert event.model_name == "openai/gpt-oss-20b"
    assert event.estimated_cost == Decimal("0.67500000")
