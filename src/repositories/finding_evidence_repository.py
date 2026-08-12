from dataclasses import dataclass

from psycopg import Connection

_FINDING_EVIDENCE_COLUMNS = """
    finding_id,
    evidence_span_id
"""


@dataclass(frozen=True)
class FindingEvidenceRecord:
    finding_id: str
    evidence_span_id: str

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "FindingEvidenceRecord":
        return cls(
            finding_id=str(row[0]),
            evidence_span_id=str(row[1]),
        )


class FindingEvidenceRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, finding_id: str, evidence_span_id: str) -> FindingEvidenceRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO finding_evidence (
                finding_id,
                evidence_span_id
            )
            VALUES (%s::uuid, %s::uuid)
            RETURNING {_FINDING_EVIDENCE_COLUMNS}
            """,
            (finding_id, evidence_span_id),
        ).fetchone()

        assert row is not None
        return FindingEvidenceRecord.from_row(row)

    def get(self, finding_id: str, evidence_span_id: str) -> FindingEvidenceRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_FINDING_EVIDENCE_COLUMNS}
            FROM finding_evidence
            WHERE finding_id = %s::uuid
              AND evidence_span_id = %s::uuid
            """,
            (finding_id, evidence_span_id),
        ).fetchone()

        return FindingEvidenceRecord.from_row(row) if row is not None else None

    def list_for_finding(self, finding_id: str) -> list[FindingEvidenceRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_FINDING_EVIDENCE_COLUMNS}
            FROM finding_evidence
            WHERE finding_id = %s::uuid
            ORDER BY evidence_span_id ASC
            """,
            (finding_id,),
        ).fetchall()

        return [FindingEvidenceRecord.from_row(row) for row in rows]
