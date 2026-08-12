from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

_RUN_DOCUMENT_VERSION_COLUMNS = """
    run_id,
    document_version_id,
    created_at
"""


@dataclass(frozen=True)
class RunDocumentVersionRecord:
    run_id: str
    document_version_id: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunDocumentVersionRecord":
        return cls(
            run_id=str(row[0]),
            document_version_id=str(row[1]),
            created_at=cast(datetime, row[2]),
        )


class RunDocumentVersionRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, run_id: str, document_version_id: str) -> RunDocumentVersionRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_document_versions (
                run_id,
                document_version_id
            )
            VALUES (%s::uuid, %s::uuid)
            RETURNING {_RUN_DOCUMENT_VERSION_COLUMNS}
            """,
            (run_id, document_version_id),
        ).fetchone()

        assert row is not None
        return RunDocumentVersionRecord.from_row(row)

    def get(
        self,
        run_id: str,
        document_version_id: str,
    ) -> RunDocumentVersionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_DOCUMENT_VERSION_COLUMNS}
            FROM run_document_versions
            WHERE run_id = %s::uuid
              AND document_version_id = %s::uuid
            """,
            (run_id, document_version_id),
        ).fetchone()

        return RunDocumentVersionRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunDocumentVersionRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_DOCUMENT_VERSION_COLUMNS}
            FROM run_document_versions
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, document_version_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunDocumentVersionRecord.from_row(row) for row in rows]
