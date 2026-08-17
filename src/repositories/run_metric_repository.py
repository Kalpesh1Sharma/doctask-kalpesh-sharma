"""Persistence-only access to truthful RunStep duration metrics."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

from psycopg import Connection

from domain.run import RunStage

_COLUMNS = """
    id, run_id, stage, attempt, duration_ms, input_tokens, output_tokens,
    estimated_cost, created_at
"""


@dataclass(frozen=True)
class RunMetricRecord:
    metric_id: str
    run_id: str
    stage: RunStage
    attempt: int
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunMetricRecord":
        return cls(
            metric_id=str(row[0]), run_id=str(row[1]), stage=RunStage(str(row[2])),
            attempt=int(row[3]), duration_ms=int(row[4]),
            input_tokens=cast(int | None, row[5]), output_tokens=cast(int | None, row[6]),
            estimated_cost=cast(Decimal | None, row[7]), created_at=cast(datetime, row[8]),
        )


class RunMetricRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create_for_closed_step(self, run_id: str, stage: RunStage, attempt: int) -> RunMetricRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
            SELECT run_id, stage, attempt,
                   FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT
            FROM run_steps
            WHERE run_id = %s::uuid AND stage = %s::run_stage AND attempt = %s
            ON CONFLICT (run_id, stage, attempt) DO NOTHING
            RETURNING {_COLUMNS}
            """, (run_id, stage.value, attempt),
        ).fetchone()
        if row is not None:
            return RunMetricRecord.from_row(row)
        existing = self.get_for_run_stage_attempt(run_id, stage, attempt)
        if existing is None:
            raise RuntimeError("closed RunStep metric was not created")
        return existing

    def get_for_run_stage_attempt(self, run_id: str, stage: RunStage, attempt: int) -> RunMetricRecord | None:
        row = self._connection.execute(
            f"""SELECT {_COLUMNS} FROM run_metrics
                WHERE run_id = %s::uuid AND stage = %s::run_stage AND attempt = %s""",
            (run_id, stage.value, attempt),
        ).fetchone()
        return RunMetricRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunMetricRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_COLUMNS}
            FROM run_metrics
            WHERE run_id = %s::uuid
            ORDER BY stage ASC, attempt ASC
            """,
            (run_id,),
        ).fetchall()
        return [RunMetricRecord.from_row(row) for row in rows]

    def set_usage(
        self,
        run_id: str,
        stage: RunStage,
        attempt: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        estimated_cost: Decimal | None,
    ) -> RunMetricRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE run_metrics
            SET input_tokens = %s, output_tokens = %s, estimated_cost = %s
            WHERE run_id = %s::uuid AND stage = %s::run_stage AND attempt = %s
            RETURNING {_COLUMNS}
            """,
            (input_tokens, output_tokens, estimated_cost, run_id, stage.value, attempt),
        ).fetchone()
        return RunMetricRecord.from_row(row) if row is not None else None
