from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from domain.fact import Fact, FactStatus

_FACT_COLUMNS = """
    id,
    origin_run_id,
    subject,
    predicate,
    object_value,
    status,
    created_at
"""


@dataclass(frozen=True)
class FactRecord:
    fact_id: str
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "FactRecord":
        return cls(
            fact_id=str(row[0]),
            origin_run_id=str(row[1]),
            subject=cast(str, row[2]),
            predicate=cast(str, row[3]),
            object_value=cast(JsonValue, row[4]),
            status=FactStatus(str(row[5])),
            created_at=cast(datetime, row[6]),
        )

    def to_domain(self) -> Fact:
        return Fact(
            fact_id=self.fact_id,
            origin_run_id=self.origin_run_id,
            subject=self.subject,
            predicate=self.predicate,
            object_value=self.object_value,
            status=self.status,
        )


class FactRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, fact: Fact) -> FactRecord:
        object_value = fact.model_dump(mode="json")["object_value"]
        row = self._connection.execute(
            f"""
            INSERT INTO facts (
                id,
                origin_run_id,
                subject,
                predicate,
                object_value,
                status
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s,
                %s,
                %s::jsonb,
                %s::fact_status
            )
            RETURNING {_FACT_COLUMNS}
            """,
            (
                fact.fact_id,
                fact.origin_run_id,
                fact.subject,
                fact.predicate,
                Jsonb(object_value),
                fact.status.value,
            ),
        ).fetchone()

        assert row is not None
        return FactRecord.from_row(row)

    def get_by_id(self, fact_id: str) -> FactRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_FACT_COLUMNS}
            FROM facts
            WHERE id = %s::uuid
            """,
            (fact_id,),
        ).fetchone()

        return FactRecord.from_row(row) if row is not None else None

    def list_for_origin_run(self, run_id: str) -> list[FactRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_FACT_COLUMNS}
            FROM facts
            WHERE origin_run_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()

        return [FactRecord.from_row(row) for row in rows]
