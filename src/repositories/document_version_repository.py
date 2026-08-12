from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.document_version import DocumentProcessingStatus, DocumentVersion

_DOCUMENT_VERSION_COLUMNS = """
    id,
    document_id,
    version_number,
    content_hash,
    storage_uri,
    mime_type,
    size_bytes,
    processing_status,
    created_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class DocumentVersionRecord:
    document_version_id: str
    document_id: str
    version_number: int
    content_hash: str
    storage_uri: str
    mime_type: str | None
    size_bytes: int | None
    processing_status: DocumentProcessingStatus
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "DocumentVersionRecord":
        return cls(
            document_version_id=str(row[0]),
            document_id=str(row[1]),
            version_number=cast(int, row[2]),
            content_hash=cast(str, row[3]),
            storage_uri=cast(str, row[4]),
            mime_type=_optional_string(row[5]),
            size_bytes=cast(int | None, row[6]),
            processing_status=DocumentProcessingStatus(str(row[7])),
            created_at=cast(datetime, row[8]),
        )

    def to_domain(self) -> DocumentVersion:
        return DocumentVersion(
            document_version_id=self.document_version_id,
            document_id=self.document_id,
            version_number=self.version_number,
            content_hash=self.content_hash,
            storage_uri=self.storage_uri,
            processing_status=self.processing_status,
        )


class DocumentVersionRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        version: DocumentVersion,
        mime_type: str | None,
        size_bytes: int | None,
    ) -> DocumentVersionRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO document_versions (
                id,
                document_id,
                version_number,
                content_hash,
                storage_uri,
                mime_type,
                size_bytes,
                processing_status
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s::document_version_status
            )
            RETURNING {_DOCUMENT_VERSION_COLUMNS}
            """,
            (
                version.document_version_id,
                version.document_id,
                version.version_number,
                version.content_hash,
                version.storage_uri,
                mime_type,
                size_bytes,
                version.processing_status.value,
            ),
        ).fetchone()

        assert row is not None
        return DocumentVersionRecord.from_row(row)

    def get_by_id(self, document_version_id: str) -> DocumentVersionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_VERSION_COLUMNS}
            FROM document_versions
            WHERE id = %s::uuid
            """,
            (document_version_id,),
        ).fetchone()

        return DocumentVersionRecord.from_row(row) if row is not None else None

    def list_for_document(self, document_id: str) -> list[DocumentVersionRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_VERSION_COLUMNS}
            FROM document_versions
            WHERE document_id = %s::uuid
            ORDER BY version_number ASC
            """,
            (document_id,),
        ).fetchall()

        return [DocumentVersionRecord.from_row(row) for row in rows]

    def next_version_number(self, document_id: str) -> int:
        row = self._connection.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) + 1
            FROM document_versions
            WHERE document_id = %s::uuid
            """,
            (document_id,),
        ).fetchone()

        assert row is not None
        return cast(int, row[0])

    def update_processing_status(
        self,
        document_version_id: str,
        status: DocumentProcessingStatus,
    ) -> DocumentVersionRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE document_versions
            SET processing_status = %s::document_version_status
            WHERE id = %s::uuid
            RETURNING {_DOCUMENT_VERSION_COLUMNS}
            """,
            (status.value, document_version_id),
        ).fetchone()

        return DocumentVersionRecord.from_row(row) if row is not None else None
