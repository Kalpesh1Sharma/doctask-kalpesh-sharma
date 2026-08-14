from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

_DOCUMENT_CHUNK_COLUMNS = """
    id,
    document_version_id,
    chunk_index,
    content,
    content_hash,
    page_number,
    start_offset,
    end_offset,
    embedding,
    created_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _vector_literal(values: Sequence[float]) -> str:
    return f"[{','.join(str(value) for value in values)}]"


def _parse_vector(value: object) -> tuple[float, ...] | None:
    if value is None:
        return None

    literal = cast(str, value)
    contents = literal[1:-1]
    return tuple(float(item) for item in contents.split(",")) if contents else ()


@dataclass(frozen=True)
class DocumentChunkRecord:
    chunk_id: str
    document_version_id: str
    chunk_index: int
    content: str
    content_hash: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    embedding: tuple[float, ...] | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "DocumentChunkRecord":
        return cls(
            chunk_id=str(row[0]),
            document_version_id=str(row[1]),
            chunk_index=cast(int, row[2]),
            content=cast(str, row[3]),
            content_hash=cast(str, row[4]),
            page_number=cast(int | None, row[5]),
            start_offset=cast(int | None, row[6]),
            end_offset=cast(int | None, row[7]),
            embedding=_parse_vector(row[8]),
            created_at=cast(datetime, row[9]),
        )


@dataclass(frozen=True)
class ChunkSearchResult:
    chunk: DocumentChunkRecord
    cosine_distance: float

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "ChunkSearchResult":
        return cls(
            chunk=DocumentChunkRecord.from_row(row[:10]),
            cosine_distance=cast(float, row[10]),
        )


class DocumentChunkRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        document_version_id: str,
        chunk_index: int,
        content: str,
        content_hash: str,
        page_number: int | None = None,
        start_offset: int | None = None,
        end_offset: int | None = None,
        embedding: Sequence[float] | None = None,
    ) -> DocumentChunkRecord:
        vector = _vector_literal(embedding) if embedding is not None else None
        row = self._connection.execute(
            f"""
            INSERT INTO document_chunks (
                document_version_id,
                chunk_index,
                content,
                content_hash,
                page_number,
                start_offset,
                end_offset,
                embedding
            )
            VALUES (
                %s::uuid,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s::vector
            )
            RETURNING {_DOCUMENT_CHUNK_COLUMNS}
            """,
            (
                document_version_id,
                chunk_index,
                content,
                content_hash,
                page_number,
                start_offset,
                end_offset,
                vector,
            ),
        ).fetchone()

        assert row is not None
        return DocumentChunkRecord.from_row(row)

    def get_by_id(self, chunk_id: str) -> DocumentChunkRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_CHUNK_COLUMNS}
            FROM document_chunks
            WHERE id = %s::uuid
            """,
            (chunk_id,),
        ).fetchone()

        return DocumentChunkRecord.from_row(row) if row is not None else None

    def list_for_document_version(
        self,
        document_version_id: str,
    ) -> list[DocumentChunkRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_CHUNK_COLUMNS}
            FROM document_chunks
            WHERE document_version_id = %s::uuid
            ORDER BY chunk_index ASC
            """,
            (document_version_id,),
        ).fetchall()

        return [DocumentChunkRecord.from_row(row) for row in rows]

    def list_for_document_version_for_share(
        self,
        document_version_id: str,
    ) -> list[DocumentChunkRecord]:
        """Return a version's chunks while blocking concurrent content updates."""
        rows = self._connection.execute(
            f"""
            SELECT {_DOCUMENT_CHUNK_COLUMNS}
            FROM document_chunks
            WHERE document_version_id = %s::uuid
            ORDER BY chunk_index ASC
            FOR SHARE
            """,
            (document_version_id,),
        ).fetchall()

        return [DocumentChunkRecord.from_row(row) for row in rows]

    def search_similar(
        self,
        query_embedding: Sequence[float],
        document_version_ids: Sequence[str],
        limit: int,
    ) -> list[ChunkSearchResult]:
        if not document_version_ids:
            return []

        vector = _vector_literal(query_embedding)
        rows = self._connection.execute(
            f"""
            SELECT
                {_DOCUMENT_CHUNK_COLUMNS},
                embedding <=> %s::vector AS cosine_distance
            FROM document_chunks
            WHERE document_version_id = ANY(%s::uuid[])
              AND embedding IS NOT NULL
            ORDER BY cosine_distance ASC, id ASC
            LIMIT %s
            """,
            (vector, list(document_version_ids), limit),
        ).fetchall()

        return [ChunkSearchResult.from_row(row) for row in rows]
