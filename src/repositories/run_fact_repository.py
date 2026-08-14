from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from pydantic import JsonValue

from domain.fact import FactStatus
from domain.run_membership import RunMembershipRelation

_RUN_FACT_COLUMNS = """
    run_id,
    fact_id,
    relation,
    created_at
"""

_RUN_FACT_RECONCILIATION_COLUMNS = """
    run_facts.run_id,
    facts.id,
    run_facts.relation,
    facts.origin_run_id,
    facts.subject,
    facts.predicate,
    facts.object_value,
    facts.status,
    evidence_spans.id,
    evidence_spans.document_version_id,
    evidence_spans.chunk_id,
    evidence_spans.page_number,
    evidence_spans.start_offset,
    evidence_spans.end_offset,
    evidence_spans.quoted_text,
    evidence_spans.content_hash
"""


@dataclass(frozen=True)
class RunFactRecord:
    run_id: str
    fact_id: str
    relation: RunMembershipRelation
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunFactRecord":
        return cls(
            run_id=str(row[0]),
            fact_id=str(row[1]),
            relation=RunMembershipRelation(str(row[2])),
            created_at=cast(datetime, row[3]),
        )


@dataclass(frozen=True)
class RunFactEvidenceRecord:
    evidence_id: str
    document_version_id: str
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str

    @classmethod
    def from_reconciliation_row(cls, row: tuple[object, ...]) -> "RunFactEvidenceRecord":
        return cls(
            evidence_id=str(row[8]),
            document_version_id=str(row[9]),
            chunk_id=str(row[10]),
            page_number=cast(int | None, row[11]),
            start_offset=cast(int | None, row[12]),
            end_offset=cast(int | None, row[13]),
            source_text=cast(str, row[14]),
            content_hash=cast(str, row[15]),
        )


@dataclass(frozen=True)
class RunFactReconciliationRecord:
    run_id: str
    fact_id: str
    relation: RunMembershipRelation
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus
    evidence: tuple[RunFactEvidenceRecord, ...]

    @classmethod
    def from_reconciliation_row(
        cls,
        row: tuple[object, ...],
        evidence: tuple[RunFactEvidenceRecord, ...],
    ) -> "RunFactReconciliationRecord":
        return cls(
            run_id=str(row[0]),
            fact_id=str(row[1]),
            relation=RunMembershipRelation(str(row[2])),
            origin_run_id=str(row[3]),
            subject=cast(str, row[4]),
            predicate=cast(str, row[5]),
            object_value=cast(JsonValue, row[6]),
            status=FactStatus(str(row[7])),
            evidence=evidence,
        )


class RunFactRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(
        self,
        run_id: str,
        fact_id: str,
        relation: RunMembershipRelation,
    ) -> RunFactRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_facts (
                run_id,
                fact_id,
                relation
            )
            VALUES (%s::uuid, %s::uuid, %s::run_membership_relation)
            RETURNING {_RUN_FACT_COLUMNS}
            """,
            (run_id, fact_id, relation.value),
        ).fetchone()

        assert row is not None
        return RunFactRecord.from_row(row)

    def get(self, run_id: str, fact_id: str) -> RunFactRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_FACT_COLUMNS}
            FROM run_facts
            WHERE run_id = %s::uuid
              AND fact_id = %s::uuid
            """,
            (run_id, fact_id),
        ).fetchone()

        return RunFactRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunFactRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_FACT_COLUMNS}
            FROM run_facts
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, fact_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunFactRecord.from_row(row) for row in rows]

    def list_reconciliation_input_for_run(
        self,
        run_id: str,
    ) -> list[RunFactReconciliationRecord]:
        """Load current-Run Facts with their exact durable supporting evidence."""
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_FACT_RECONCILIATION_COLUMNS}
            FROM run_facts
            JOIN facts ON facts.id = run_facts.fact_id
            LEFT JOIN fact_evidence ON fact_evidence.fact_id = facts.id
            LEFT JOIN evidence_spans ON evidence_spans.id = fact_evidence.evidence_span_id
            WHERE run_facts.run_id = %s::uuid
            ORDER BY facts.id ASC, evidence_spans.id ASC
            """,
            (run_id,),
        ).fetchall()

        grouped: dict[str, tuple[tuple[object, ...], list[RunFactEvidenceRecord]]] = {}
        for row in rows:
            fact_id = str(row[1])
            if fact_id not in grouped:
                grouped[fact_id] = (row, [])
            if row[8] is not None:
                grouped[fact_id][1].append(RunFactEvidenceRecord.from_reconciliation_row(row))

        return [
            RunFactReconciliationRecord.from_reconciliation_row(row, tuple(evidence))
            for row, evidence in grouped.values()
        ]
