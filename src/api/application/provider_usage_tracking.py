"""Durable provider invocation receipt handling for semantic RunStep attempts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from psycopg_pool import ConnectionPool

from config.settings import OpenAIModelPricing, get_settings
from domain.run import RunStage, RunStepStatus
from repositories.provider_usage_repository import ProviderUsageRepository
from repositories.run_step_repository import RunStepRepository

_COST_QUANTUM = Decimal("0.00000001")
_TOKENS_PER_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ProviderUsageReceipt:
    event_id: str


def _provider_identity(provider: object) -> tuple[str, str | None]:
    provider_name = getattr(provider, "provider_name", None)
    model_name = getattr(provider, "model_name", None)
    return (
        str(provider_name) if provider_name else type(provider).__name__,
        str(model_name) if model_name else None,
    )


def _estimate_openai_cost(
    *,
    provider_name: str | None,
    model_name: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    pricing: dict[str, OpenAIModelPricing],
) -> Decimal | None:
    if (
        provider_name != "openai"
        or model_name is None
        or input_tokens is None
        or output_tokens is None
    ):
        return None
    model_pricing = pricing.get(model_name)
    if model_pricing is None:
        return None
    total = (
        Decimal(input_tokens) * model_pricing.input_cost_per_million
        + Decimal(output_tokens) * model_pricing.output_cost_per_million
    ) / _TOKENS_PER_MILLION
    return total.quantize(_COST_QUANTUM)


def start_provider_usage(
    connection_pool: ConnectionPool,
    *,
    run_id: str,
    stage: RunStage,
    operation_scope: str,
    operation_key: str,
    claim_generation: int,
    provider: object,
) -> ProviderUsageReceipt | None:
    """Durably attribute a real provider call to its active workflow attempt."""
    provider_name, model_name = _provider_identity(provider)
    with connection_pool.connection() as connection:
        candidates = [
            step
            for step in RunStepRepository(connection).list_for_run(run_id)
            if step.stage is stage and step.status is RunStepStatus.RUNNING
        ]
        if not candidates:
            connection.commit()
            return None
        step = max(candidates, key=lambda item: item.attempt)
        event = ProviderUsageRepository(connection).create_started(
            run_id=run_id,
            stage=stage,
            attempt=step.attempt,
            operation_scope=operation_scope,
            operation_key=operation_key,
            claim_generation=claim_generation,
            provider_name=provider_name,
            model_name=model_name,
        )
        connection.commit()
        return ProviderUsageReceipt(event.event_id)


def finish_provider_usage(
    connection_pool: ConnectionPool,
    receipt: ProviderUsageReceipt | None,
    usage: object | None = None,
) -> None:
    if receipt is None:
        return
    with connection_pool.connection() as connection:
        # Existing provider Protocols may not expose counters. Preserve truth by
        # recording those invocations as UNAVAILABLE rather than estimating.
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        provider_name = getattr(usage, "provider_name", None)
        model_name = getattr(usage, "model_name", None)
        if input_tokens is None and output_tokens is None:
            updated = ProviderUsageRepository(connection).mark_unavailable(receipt.event_id)
        else:
            estimated_cost = _estimate_openai_cost(
                provider_name=str(provider_name) if provider_name else None,
                model_name=str(model_name) if model_name else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                pricing=get_settings().openai_model_pricing,
            )
            updated = ProviderUsageRepository(connection).record(
                receipt.event_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                provider_name=str(provider_name) if provider_name else None,
                model_name=str(model_name) if model_name else None,
            )
        if updated is None:
            raise RuntimeError("provider usage receipt disappeared")
        connection.commit()
