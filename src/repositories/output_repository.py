from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

_OUTPUT_COLUMNS = """
    id,
    run_id,
    output_type,
    storage_uri,
    content_hash,
    created_at
"""


@dataclass(frozen=True)
class OutputRecord:
    output_id: str
    run_id: str
    output_type: str
    storage_uri: str
    content_hash: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "OutputRecord":
        return cls(
            output_id=str(row[0]),
            run_id=str(row[1]),
            output_type=cast(str, row[2]),
            storage_uri=cast(str, row[3]),
            content_hash=cast(str, row[4]),
            created_at=cast(datetime, row[5]),
        )


class OutputRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        run_id: str,
        output_type: str,
        storage_uri: str,
        content_hash: str,
    ) -> OutputRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO outputs (run_id, output_type, storage_uri, content_hash)
            VALUES (%s::uuid, %s, %s, %s)
            RETURNING {_OUTPUT_COLUMNS}
            """,
            (run_id, output_type, storage_uri, content_hash),
        ).fetchone()

        assert row is not None
        return OutputRecord.from_row(row)

    def get_by_id(self, output_id: str) -> OutputRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_OUTPUT_COLUMNS}
            FROM outputs
            WHERE id = %s::uuid
            """,
            (output_id,),
        ).fetchone()

        return OutputRecord.from_row(row) if row is not None else None

    def get_by_run_and_type(self, run_id: str, output_type: str) -> OutputRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_OUTPUT_COLUMNS}
            FROM outputs
            WHERE run_id = %s::uuid AND output_type = %s
            """,
            (run_id, output_type),
        ).fetchone()

        return OutputRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[OutputRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_OUTPUT_COLUMNS}
            FROM outputs
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()

        return [OutputRecord.from_row(row) for row in rows]
