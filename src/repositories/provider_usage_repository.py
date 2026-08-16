"""Durable provider receipt attribution for semantic RunStep attempts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from psycopg import Connection

from domain.run import RunStage


class ProviderUsageState(StrEnum):
    STARTED = "STARTED"
    RECORDED = "RECORDED"
    UNAVAILABLE = "UNAVAILABLE"


_COLUMNS = """
    id, run_id, stage, attempt, operation_scope, operation_key, claim_generation,
    provider_name, model_name, state, input_tokens, output_tokens, estimated_cost,
    started_at, recorded_at, created_at
"""


@dataclass(frozen=True)
class ProviderUsageEventRecord:
    event_id: str
    run_id: str
    stage: RunStage
    attempt: int
    operation_scope: str
    operation_key: str
    claim_generation: int
    provider_name: str
    model_name: str | None
    state: ProviderUsageState
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None
    started_at: datetime
    recorded_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> ProviderUsageEventRecord:
        return cls(
            event_id=str(row[0]), run_id=str(row[1]), stage=RunStage(str(row[2])), attempt=int(row[3]),
            operation_scope=cast(str, row[4]), operation_key=cast(str, row[5]),
            claim_generation=int(row[6]), provider_name=cast(str, row[7]),
            model_name=str(row[8]) if row[8] is not None else None,
            state=ProviderUsageState(str(row[9])), input_tokens=cast(int | None, row[10]),
            output_tokens=cast(int | None, row[11]), estimated_cost=cast(Decimal | None, row[12]),
            started_at=cast(datetime, row[13]), recorded_at=cast(datetime | None, row[14]),
            created_at=cast(datetime, row[15]),
        )


class ProviderUsageRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create_started(
        self,
        *,
        run_id: str,
        stage: RunStage,
        attempt: int,
        operation_scope: str,
        operation_key: str,
        claim_generation: int,
        provider_name: str,
        model_name: str | None,
    ) -> ProviderUsageEventRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO provider_usage_events (
                run_id, stage, attempt, operation_scope, operation_key,
                claim_generation, provider_name, model_name, state
            ) VALUES (%s::uuid, %s::run_stage, %s, %s, %s, %s, %s, %s,
                      'STARTED'::provider_usage_state)
            ON CONFLICT (run_id, stage, attempt, operation_scope, operation_key, claim_generation)
            DO UPDATE SET provider_name = provider_usage_events.provider_name
            RETURNING {_COLUMNS}
            """,
            (run_id, stage.value, attempt, operation_scope, operation_key,
             claim_generation, provider_name, model_name),
        ).fetchone()
        assert row is not None
        return ProviderUsageEventRecord.from_row(row)

    def record(
        self,
        event_id: str,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost: Decimal | None,
        provider_name: str | None = None,
        model_name: str | None = None,
    ) -> ProviderUsageEventRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE provider_usage_events
            SET state = 'RECORDED'::provider_usage_state,
                provider_name = COALESCE(%s, provider_name),
                model_name = COALESCE(%s, model_name),
                input_tokens = %s,
                output_tokens = %s,
                estimated_cost = %s,
                recorded_at = COALESCE(recorded_at, clock_timestamp())
            WHERE id = %s::uuid
            RETURNING {_COLUMNS}
            """,
            (provider_name, model_name, input_tokens, output_tokens, estimated_cost, event_id),
        ).fetchone()
        return ProviderUsageEventRecord.from_row(row) if row is not None else None

    def mark_unavailable(self, event_id: str) -> ProviderUsageEventRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE provider_usage_events
            SET state = 'UNAVAILABLE'::provider_usage_state,
                recorded_at = COALESCE(recorded_at, clock_timestamp())
            WHERE id = %s::uuid
            RETURNING {_COLUMNS}
            """,
            (event_id,),
        ).fetchone()
        return ProviderUsageEventRecord.from_row(row) if row is not None else None

    def list_for_step(self, run_id: str, stage: RunStage, attempt: int) -> tuple[ProviderUsageEventRecord, ...]:
        rows = self._connection.execute(
            f"""
            SELECT {_COLUMNS} FROM provider_usage_events
            WHERE run_id = %s::uuid AND stage = %s::run_stage AND attempt = %s
            ORDER BY operation_scope, operation_key, claim_generation
            """,
            (run_id, stage.value, attempt),
        ).fetchall()
        return tuple(ProviderUsageEventRecord.from_row(row) for row in rows)

    def aggregate_for_step(
        self,
        run_id: str,
        stage: RunStage,
        attempt: int,
    ) -> tuple[int | None, int | None, Decimal | None]:
        row = self._connection.execute(
            """
            SELECT
                CASE WHEN count(*) = 0
                          OR count(*) FILTER (WHERE state <> 'RECORDED') > 0
                          OR count(*) FILTER (WHERE input_tokens IS NULL) > 0
                     THEN NULL ELSE COALESCE(sum(input_tokens), 0) END,
                CASE WHEN count(*) = 0
                          OR count(*) FILTER (WHERE state <> 'RECORDED') > 0
                          OR count(*) FILTER (WHERE output_tokens IS NULL) > 0
                     THEN NULL ELSE COALESCE(sum(output_tokens), 0) END,
                CASE WHEN count(*) = 0
                          OR count(*) FILTER (WHERE state <> 'RECORDED') > 0
                          OR count(*) FILTER (WHERE estimated_cost IS NULL) > 0
                     THEN NULL ELSE COALESCE(sum(estimated_cost), 0) END
            FROM provider_usage_events
            WHERE run_id = %s::uuid AND stage = %s::run_stage AND attempt = %s
            """,
            (run_id, stage.value, attempt),
        ).fetchone()
        assert row is not None
        return cast(int | None, row[0]), cast(int | None, row[1]), cast(Decimal | None, row[2])
