"""Persistence for immutable semantic DocumentVersion classifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.document_classification import DocumentRole

_CLASSIFICATION_COLUMNS = """
    id,
    document_version_id,
    role,
    confidence,
    explanation,
    input_fingerprint,
    provider_name,
    model_name,
    input_tokens,
    output_tokens,
    created_at
"""


@dataclass(frozen=True)
class DocumentClassificationRecord:
    classification_id: str
    document_version_id: str
    role: DocumentRole
    confidence: float
    explanation: str
    input_fingerprint: str
    provider_name: str
    model_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    evidence_chunk_ids: tuple[str, ...]
    created_at: datetime


class DocumentClassificationRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_by_document_version(
        self,
        document_version_id: str,
    ) -> DocumentClassificationRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_CLASSIFICATION_COLUMNS}
            FROM document_classifications
            WHERE document_version_id = %s::uuid
            """,
            (document_version_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def get_by_document_version_for_update(
        self,
        document_version_id: str,
    ) -> DocumentClassificationRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_CLASSIFICATION_COLUMNS}
            FROM document_classifications
            WHERE document_version_id = %s::uuid
            FOR SHARE
            """,
            (document_version_id,),
        ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def create(
        self,
        *,
        document_version_id: str,
        role: DocumentRole,
        confidence: float,
        explanation: str,
        input_fingerprint: str,
        provider_name: str,
        model_name: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        evidence_chunk_ids: tuple[str, ...],
    ) -> DocumentClassificationRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO document_classifications (
                document_version_id,
                role,
                confidence,
                explanation,
                input_fingerprint,
                provider_name,
                model_name,
                input_tokens,
                output_tokens
            )
            VALUES (
                %s::uuid,
                %s::document_role,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
            RETURNING {_CLASSIFICATION_COLUMNS}
            """,
            (
                document_version_id,
                role.value,
                confidence,
                explanation,
                input_fingerprint,
                provider_name,
                model_name,
                input_tokens,
                output_tokens,
            ),
        ).fetchone()
        assert row is not None
        classification_id = str(row[0])
        for order, chunk_id in enumerate(evidence_chunk_ids):
            self._connection.execute(
                """
                INSERT INTO document_classification_evidence (
                    classification_id,
                    chunk_id,
                    evidence_order
                )
                VALUES (%s::uuid, %s::uuid, %s)
                """,
                (classification_id, chunk_id, order),
            )
        return self._from_row(row)

    def _from_row(self, row: tuple[object, ...]) -> DocumentClassificationRecord:
        classification_id = str(row[0])
        evidence_rows = self._connection.execute(
            """
            SELECT chunk_id
            FROM document_classification_evidence
            WHERE classification_id = %s::uuid
            ORDER BY evidence_order ASC
            """,
            (classification_id,),
        ).fetchall()
        return DocumentClassificationRecord(
            classification_id=classification_id,
            document_version_id=str(row[1]),
            role=DocumentRole(str(row[2])),
            confidence=float(row[3]),
            explanation=cast(str, row[4]),
            input_fingerprint=cast(str, row[5]),
            provider_name=cast(str, row[6]),
            model_name=cast(str | None, row[7]),
            input_tokens=cast(int | None, row[8]),
            output_tokens=cast(int | None, row[9]),
            evidence_chunk_ids=tuple(str(item[0]) for item in evidence_rows),
            created_at=cast(datetime, row[10]),
        )
