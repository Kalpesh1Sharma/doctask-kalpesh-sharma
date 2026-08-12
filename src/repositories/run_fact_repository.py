from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.run_membership import RunMembershipRelation

_RUN_FACT_COLUMNS = """
    run_id,
    fact_id,
    relation,
    created_at
"""


@dataclass(frozen=True)
class RunFactRecord:
    run_id: str
    fact_id: str
    relation: RunMembershipRelation
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunFactRecord":
        return cls(
            run_id=str(row[0]),
            fact_id=str(row[1]),
            relation=RunMembershipRelation(str(row[2])),
            created_at=cast(datetime, row[3]),
        )


class RunFactRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(
        self,
        run_id: str,
        fact_id: str,
        relation: RunMembershipRelation,
    ) -> RunFactRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_facts (
                run_id,
                fact_id,
                relation
            )
            VALUES (%s::uuid, %s::uuid, %s::run_membership_relation)
            RETURNING {_RUN_FACT_COLUMNS}
            """,
            (run_id, fact_id, relation.value),
        ).fetchone()

        assert row is not None
        return RunFactRecord.from_row(row)

    def get(self, run_id: str, fact_id: str) -> RunFactRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_FACT_COLUMNS}
            FROM run_facts
            WHERE run_id = %s::uuid
              AND fact_id = %s::uuid
            """,
            (run_id, fact_id),
        ).fetchone()

        return RunFactRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunFactRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_FACT_COLUMNS}
            FROM run_facts
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, fact_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunFactRecord.from_row(row) for row in rows]
