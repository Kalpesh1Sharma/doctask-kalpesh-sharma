from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.evidence import EvidenceSpan

_EVIDENCE_SPAN_COLUMNS = """
    id,
    document_version_id,
    chunk_id,
    page_number,
    start_offset,
    end_offset,
    quoted_text,
    content_hash,
    created_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class EvidenceSpanRecord:
    evidence_id: str
    document_version_id: str
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "EvidenceSpanRecord":
        return cls(
            evidence_id=str(row[0]),
            document_version_id=str(row[1]),
            chunk_id=str(row[2]),
            page_number=cast(int | None, row[3]),
            start_offset=cast(int | None, row[4]),
            end_offset=cast(int | None, row[5]),
            source_text=cast(str, row[6]),
            content_hash=cast(str, row[7]),
            created_at=cast(datetime, row[8]),
        )

    def to_domain(self) -> EvidenceSpan:
        return EvidenceSpan(
            evidence_id=self.evidence_id,
            document_version_id=self.document_version_id,
            chunk_id=self.chunk_id,
            page_number=self.page_number,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            source_text=self.source_text,
            content_hash=self.content_hash,
        )


class EvidenceSpanRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, span: EvidenceSpan) -> EvidenceSpanRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO evidence_spans (
                id,
                document_version_id,
                chunk_id,
                page_number,
                start_offset,
                end_offset,
                quoted_text,
                content_hash
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s::uuid,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            RETURNING {_EVIDENCE_SPAN_COLUMNS}
            """,
            (
                span.evidence_id,
                span.document_version_id,
                span.chunk_id,
                span.page_number,
                span.start_offset,
                span.end_offset,
                span.source_text,
                span.content_hash,
            ),
        ).fetchone()

        assert row is not None
        return EvidenceSpanRecord.from_row(row)

    def get_by_id(self, evidence_id: str) -> EvidenceSpanRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_EVIDENCE_SPAN_COLUMNS}
            FROM evidence_spans
            WHERE id = %s::uuid
            """,
            (evidence_id,),
        ).fetchone()

        return EvidenceSpanRecord.from_row(row) if row is not None else None

    def list_for_document_version(
        self,
        document_version_id: str,
    ) -> list[EvidenceSpanRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_EVIDENCE_SPAN_COLUMNS}
            FROM evidence_spans
            WHERE document_version_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (document_version_id,),
        ).fetchall()

        return [EvidenceSpanRecord.from_row(row) for row in rows]
