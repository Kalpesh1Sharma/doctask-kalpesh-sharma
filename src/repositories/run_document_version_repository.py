from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.document_version import DocumentProcessingStatus

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


@dataclass(frozen=True)
class RunDocumentVersionScopeRecord:
    run_id: str
    document_version_id: str
    document_id: str
    document_pile_id: str
    version_number: int
    content_hash: str
    processing_status: DocumentProcessingStatus

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunDocumentVersionScopeRecord":
        return cls(
            run_id=str(row[0]),
            document_version_id=str(row[1]),
            document_id=str(row[2]),
            document_pile_id=str(row[3]),
            version_number=cast(int, row[4]),
            content_hash=cast(str, row[5]),
            processing_status=DocumentProcessingStatus(str(row[6])),
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

    def list_scope_for_run(self, run_id: str) -> list[RunDocumentVersionScopeRecord]:
        """Load deterministic document identity and readiness for a Run input scope."""
        rows = self._connection.execute(
            """
            SELECT
                run_document_versions.run_id,
                document_versions.id,
                documents.id,
                documents.pile_id,
                document_versions.version_number,
                document_versions.content_hash,
                document_versions.processing_status
            FROM run_document_versions
            JOIN document_versions
              ON document_versions.id = run_document_versions.document_version_id
            JOIN documents ON documents.id = document_versions.document_id
            WHERE run_document_versions.run_id = %s::uuid
            ORDER BY documents.id ASC, document_versions.version_number ASC, document_versions.id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunDocumentVersionScopeRecord.from_row(row) for row in rows]
