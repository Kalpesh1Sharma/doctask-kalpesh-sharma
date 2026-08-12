from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.run_membership import RunMembershipRelation

_RUN_FINDING_COLUMNS = """
    run_id,
    finding_id,
    relation,
    created_at
"""


@dataclass(frozen=True)
class RunFindingRecord:
    run_id: str
    finding_id: str
    relation: RunMembershipRelation
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunFindingRecord":
        return cls(
            run_id=str(row[0]),
            finding_id=str(row[1]),
            relation=RunMembershipRelation(str(row[2])),
            created_at=cast(datetime, row[3]),
        )


class RunFindingRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(
        self,
        run_id: str,
        finding_id: str,
        relation: RunMembershipRelation,
    ) -> RunFindingRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_findings (
                run_id,
                finding_id,
                relation
            )
            VALUES (%s::uuid, %s::uuid, %s::run_membership_relation)
            RETURNING {_RUN_FINDING_COLUMNS}
            """,
            (run_id, finding_id, relation.value),
        ).fetchone()

        assert row is not None
        return RunFindingRecord.from_row(row)

    def get(self, run_id: str, finding_id: str) -> RunFindingRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_FINDING_COLUMNS}
            FROM run_findings
            WHERE run_id = %s::uuid
              AND finding_id = %s::uuid
            """,
            (run_id, finding_id),
        ).fetchone()

        return RunFindingRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunFindingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_FINDING_COLUMNS}
            FROM run_findings
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, finding_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunFindingRecord.from_row(row) for row in rows]
