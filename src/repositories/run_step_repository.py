from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.run import RunStage, RunStepStatus

_RUN_STEP_COLUMNS = """
    id,
    run_id,
    stage,
    status,
    attempt,
    started_at,
    completed_at,
    error_code,
    error_message,
    created_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class RunStepRecord:
    step_id: str
    run_id: str
    stage: RunStage
    status: RunStepStatus
    attempt: int
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunStepRecord":
        return cls(
            step_id=str(row[0]),
            run_id=str(row[1]),
            stage=RunStage(str(row[2])),
            status=RunStepStatus(str(row[3])),
            attempt=cast(int, row[4]),
            started_at=cast(datetime | None, row[5]),
            completed_at=cast(datetime | None, row[6]),
            error_code=_optional_string(row[7]),
            error_message=_optional_string(row[8]),
            created_at=cast(datetime, row[9]),
        )


class RunStepRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        run_id: str,
        stage: RunStage,
        attempt: int,
    ) -> RunStepRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_steps (
                run_id,
                stage,
                attempt
            )
            VALUES (%s::uuid, %s::run_stage, %s)
            RETURNING {_RUN_STEP_COLUMNS}
            """,
            (run_id, stage.value, attempt),
        ).fetchone()

        assert row is not None
        return RunStepRecord.from_row(row)

    def get_by_id(self, step_id: str) -> RunStepRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_STEP_COLUMNS}
            FROM run_steps
            WHERE id = %s::uuid
            """,
            (step_id,),
        ).fetchone()

        return RunStepRecord.from_row(row) if row is not None else None

    def get_for_run_stage_attempt(
        self,
        run_id: str,
        stage: RunStage,
        attempt: int,
    ) -> RunStepRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_STEP_COLUMNS}
            FROM run_steps
            WHERE run_id = %s::uuid
              AND stage = %s::run_stage
              AND attempt = %s
            """,
            (run_id, stage.value, attempt),
        ).fetchone()

        return RunStepRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunStepRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_STEP_COLUMNS}
            FROM run_steps
            WHERE run_id = %s::uuid
            ORDER BY stage ASC, attempt ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunStepRecord.from_row(row) for row in rows]

    def update_status(
        self,
        step_id: str,
        status: RunStepStatus,
    ) -> RunStepRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE run_steps
            SET status = %s::run_step_status
            WHERE id = %s::uuid
            RETURNING {_RUN_STEP_COLUMNS}
            """,
            (status.value, step_id),
        ).fetchone()

        return RunStepRecord.from_row(row) if row is not None else None

    def update_execution_state(
        self,
        step_id: str,
        status: RunStepStatus,
        *,
        mark_started: bool = False,
        mark_completed: bool = False,
        error_code: str | None = None,
        error_message: str | None = None,
        clear_error: bool = False,
    ) -> RunStepRecord | None:
        """Persist caller-selected execution metadata with database timestamps.

        The caller owns lifecycle policy and transaction boundaries.  This method
        deliberately exposes the requested status transition to PostgreSQL's
        trigger instead of synthesizing a transition path in Python.
        """
        row = self._connection.execute(
            f"""
            UPDATE run_steps
            SET
                status = %s::run_step_status,
                started_at = CASE
                    WHEN %s THEN COALESCE(started_at, clock_timestamp())
                    ELSE started_at
                END,
                completed_at = CASE
                    WHEN %s THEN COALESCE(completed_at, clock_timestamp())
                    ELSE completed_at
                END,
                error_code = CASE
                    WHEN %s THEN NULL
                    ELSE %s
                END,
                error_message = CASE
                    WHEN %s THEN NULL
                    ELSE %s
                END
            WHERE id = %s::uuid
            RETURNING {_RUN_STEP_COLUMNS}
            """,
            (
                status.value,
                mark_started,
                mark_completed,
                clear_error,
                error_code,
                clear_error,
                error_message,
                step_id,
            ),
        ).fetchone()

        return RunStepRecord.from_row(row) if row is not None else None
