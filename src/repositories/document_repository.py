from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

_DOCUMENT_COLUMNS = """
    id,
    pile_id,
    logical_key,
    current_version_id,
    created_at,
    updated_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class DocumentRecord:
    document_id: str
    pile_id: str
    logical_key: str
    current_version_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "DocumentRecord":
        return cls(
            document_id=str(row[0]),
            pile_id=str(row[1]),
            logical_key=cast(str, row[2]),
            current_version_id=_optional_string(row[3]),
            created_at=cast(datetime, row[4]),
            updated_at=cast(datetime, row[5]),
        )


@dataclass(frozen=True)
class CurrentDocumentVersionRecord:
    document_id: str
    pile_id: str
    logical_key: str
    document_version_id: str
    version_number: int
    processing_status: str

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "CurrentDocumentVersionRecord":
        return cls(
            document_id=str(row[0]),
            pile_id=str(row[1]),
            logical_key=cast(str, row[2]),
            document_version_id=str(row[3]),
            version_number=cast(int, row[4]),
            processing_status=cast(str, row[5]),
        )


class DocumentRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, pile_id: str, logical_key: str) -> DocumentRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO documents (
                pile_id,
                logical_key
            )
            VALUES (%s::uuid, %s)
            RETURNING {_DOCUMENT_COLUMNS}
            """,
            (pile_id, logical_key),
        ).fetchone()

        assert row is not None
        return DocumentRecord.from_row(row)

    def get_by_id(self, document_id: str) -> DocumentRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM documents
            WHERE id = %s::uuid
            """,
            (document_id,),
        ).fetchone()

        return DocumentRecord.from_row(row) if row is not None else None

    def get_by_logical_key(
        self,
        pile_id: str,
        logical_key: str,
    ) -> DocumentRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM documents
            WHERE pile_id = %s::uuid
              AND logical_key = %s
            """,
            (pile_id, logical_key),
        ).fetchone()

        return DocumentRecord.from_row(row) if row is not None else None

    def get_for_update(self, document_id: str) -> DocumentRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM documents
            WHERE id = %s::uuid
            FOR UPDATE
            """,
            (document_id,),
        ).fetchone()

        return DocumentRecord.from_row(row) if row is not None else None

    def create_or_get_for_update(self, pile_id: str, logical_key: str) -> DocumentRecord:
        """Serialize version allocation on the durable logical-document row."""
        row = self._connection.execute(
            f"""
            INSERT INTO documents (pile_id, logical_key)
            VALUES (%s::uuid, %s)
            ON CONFLICT (pile_id, logical_key) DO NOTHING
            RETURNING {_DOCUMENT_COLUMNS}
            """,
            (pile_id, logical_key),
        ).fetchone()
        if row is not None:
            return DocumentRecord.from_row(row)
        existing = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM documents
            WHERE pile_id = %s::uuid AND logical_key = %s
            FOR UPDATE
            """,
            (pile_id, logical_key),
        ).fetchone()
        assert existing is not None
        return DocumentRecord.from_row(existing)

    def set_current_version(
        self,
        document_id: str,
        current_version_id: str,
    ) -> DocumentRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE documents
            SET current_version_id = %s::uuid
            WHERE id = %s::uuid
            RETURNING {_DOCUMENT_COLUMNS}
            """,
            (current_version_id, document_id),
        ).fetchone()

        return DocumentRecord.from_row(row) if row is not None else None

    def list_current_versions_for_pile(self, pile_id: str) -> list[CurrentDocumentVersionRecord]:
        rows = self._connection.execute(
            """
            SELECT documents.id, documents.pile_id, documents.logical_key,
                   document_versions.id, document_versions.version_number,
                   document_versions.processing_status
            FROM documents
            JOIN document_versions ON document_versions.id = documents.current_version_id
            WHERE documents.pile_id = %s::uuid
            ORDER BY documents.id ASC, document_versions.version_number ASC, document_versions.id ASC
            """,
            (pile_id,),
        ).fetchall()
        return [CurrentDocumentVersionRecord.from_row(row) for row in rows]
