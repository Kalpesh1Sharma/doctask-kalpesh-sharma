from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.run import Run, RunStatus, RunType

_RUN_COLUMNS = """
    id,
    pile_id,
    run_type,
    status,
    trigger_document_version_id,
    parent_run_id,
    baseline_run_id,
    blocked_on_run_id,
    started_at,
    completed_at,
    created_at,
    updated_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    pile_id: str
    run_type: RunType
    status: RunStatus
    trigger_document_version_id: str | None
    parent_run_id: str | None
    baseline_run_id: str | None
    blocked_on_run_id: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunRecord":
        return cls(
            run_id=str(row[0]),
            pile_id=str(row[1]),
            run_type=RunType(str(row[2])),
            status=RunStatus(str(row[3])),
            trigger_document_version_id=_optional_string(row[4]),
            parent_run_id=_optional_string(row[5]),
            baseline_run_id=_optional_string(row[6]),
            blocked_on_run_id=_optional_string(row[7]),
            started_at=cast(datetime | None, row[8]),
            completed_at=cast(datetime | None, row[9]),
            created_at=cast(datetime, row[10]),
            updated_at=cast(datetime, row[11]),
        )

    def to_domain(self) -> Run:
        return Run(
            run_id=self.run_id,
            pile_id=self.pile_id,
            run_type=self.run_type,
            status=self.status,
            trigger_document_version_id=self.trigger_document_version_id,
            parent_run_id=self.parent_run_id,
            baseline_run_id=self.baseline_run_id,
            blocked_on_run_id=self.blocked_on_run_id,
        )


class RunRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, run: Run) -> RunRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO runs (
                id,
                pile_id,
                run_type,
                status,
                trigger_document_version_id,
                parent_run_id,
                baseline_run_id,
                blocked_on_run_id
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s::run_type,
                %s::run_status,
                %s::uuid,
                %s::uuid,
                %s::uuid,
                %s::uuid
            )
            RETURNING {_RUN_COLUMNS}
            """,
            (
                run.run_id,
                run.pile_id,
                run.run_type.value,
                run.status.value,
                run.trigger_document_version_id,
                run.parent_run_id,
                run.baseline_run_id,
                run.blocked_on_run_id,
            ),
        ).fetchone()

        assert row is not None
        return RunRecord.from_row(row)

    def get_by_id(self, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE id = %s::uuid
            """,
            (run_id,),
        ).fetchone()

        return RunRecord.from_row(row) if row is not None else None

    def get_for_update(self, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE id = %s::uuid
            FOR UPDATE
            """,
            (run_id,),
        ).fetchone()

        return RunRecord.from_row(row) if row is not None else None

    def update_status(self, run_id: str, status: RunStatus) -> RunRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE runs
            SET
                status = %s::run_status,
                completed_at = CASE
                    WHEN %s::run_status = 'COMPLETED'
                    THEN COALESCE(completed_at, now())
                    ELSE completed_at
                END
            WHERE id = %s::uuid
            RETURNING {_RUN_COLUMNS}
            """,
            (status.value, status.value, run_id),
        ).fetchone()

        return RunRecord.from_row(row) if row is not None else None
