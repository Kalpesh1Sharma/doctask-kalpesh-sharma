from dataclasses import dataclass

from psycopg import Connection

_FACT_EVIDENCE_COLUMNS = """
    fact_id,
    evidence_span_id
"""


@dataclass(frozen=True)
class FactEvidenceRecord:
    fact_id: str
    evidence_span_id: str

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "FactEvidenceRecord":
        return cls(
            fact_id=str(row[0]),
            evidence_span_id=str(row[1]),
        )


class FactEvidenceRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, fact_id: str, evidence_span_id: str) -> FactEvidenceRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO fact_evidence (
                fact_id,
                evidence_span_id
            )
            VALUES (%s::uuid, %s::uuid)
            RETURNING {_FACT_EVIDENCE_COLUMNS}
            """,
            (fact_id, evidence_span_id),
        ).fetchone()

        assert row is not None
        return FactEvidenceRecord.from_row(row)

    def get(self, fact_id: str, evidence_span_id: str) -> FactEvidenceRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_FACT_EVIDENCE_COLUMNS}
            FROM fact_evidence
            WHERE fact_id = %s::uuid
              AND evidence_span_id = %s::uuid
            """,
            (fact_id, evidence_span_id),
        ).fetchone()

        return FactEvidenceRecord.from_row(row) if row is not None else None

    def list_for_fact(self, fact_id: str) -> list[FactEvidenceRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_FACT_EVIDENCE_COLUMNS}
            FROM fact_evidence
            WHERE fact_id = %s::uuid
            ORDER BY evidence_span_id ASC
            """,
            (fact_id,),
        ).fetchall()

        return [FactEvidenceRecord.from_row(row) for row in rows]
