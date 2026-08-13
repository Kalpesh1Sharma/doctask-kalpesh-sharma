from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.finding import FindingStatus, FindingType
from domain.run_membership import RunMembershipRelation

_RUN_FINDING_COLUMNS = """
    run_id,
    finding_id,
    relation,
    created_at
"""

_RUN_FINDING_REVIEW_STATE_COLUMNS = """
    run_findings.run_id,
    findings.id,
    run_findings.relation,
    findings.origin_run_id,
    findings.finding_type,
    findings.rule_id,
    findings.title,
    findings.description,
    findings.severity,
    findings.status
"""


@dataclass(frozen=True)
class RunFindingRecord:
    run_id: str
    finding_id: str
    relation: RunMembershipRelation
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunFindingRecord":
        return cls(
            run_id=str(row[0]),
            finding_id=str(row[1]),
            relation=RunMembershipRelation(str(row[2])),
            created_at=cast(datetime, row[3]),
        )


@dataclass(frozen=True)
class RunFindingReviewStateRecord:
    run_id: str
    finding_id: str
    relation: RunMembershipRelation
    origin_run_id: str
    finding_type: FindingType
    rule_id: str | None
    title: str
    description: str | None
    severity: str
    status: FindingStatus

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunFindingReviewStateRecord":
        return cls(
            run_id=str(row[0]),
            finding_id=str(row[1]),
            relation=RunMembershipRelation(str(row[2])),
            origin_run_id=str(row[3]),
            finding_type=FindingType(str(row[4])),
            rule_id=str(row[5]) if row[5] is not None else None,
            title=cast(str, row[6]),
            description=cast(str | None, row[7]),
            severity=cast(str, row[8]),
            status=FindingStatus(str(row[9])),
        )


class RunFindingRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(
        self,
        run_id: str,
        finding_id: str,
        relation: RunMembershipRelation,
    ) -> RunFindingRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_findings (
                run_id,
                finding_id,
                relation
            )
            VALUES (%s::uuid, %s::uuid, %s::run_membership_relation)
            RETURNING {_RUN_FINDING_COLUMNS}
            """,
            (run_id, finding_id, relation.value),
        ).fetchone()

        assert row is not None
        return RunFindingRecord.from_row(row)

    def get(self, run_id: str, finding_id: str) -> RunFindingRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_FINDING_COLUMNS}
            FROM run_findings
            WHERE run_id = %s::uuid
              AND finding_id = %s::uuid
            """,
            (run_id, finding_id),
        ).fetchone()

        return RunFindingRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[RunFindingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_FINDING_COLUMNS}
            FROM run_findings
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, finding_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunFindingRecord.from_row(row) for row in rows]

    def list_review_state_for_run(self, run_id: str) -> list[RunFindingReviewStateRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_FINDING_REVIEW_STATE_COLUMNS}
            FROM run_findings
            JOIN findings ON findings.id = run_findings.finding_id
            WHERE run_findings.run_id = %s::uuid
            ORDER BY findings.id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunFindingReviewStateRecord.from_row(row) for row in rows]

    def has_pending_for_run(self, run_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM run_findings
                JOIN findings ON findings.id = run_findings.finding_id
                WHERE run_findings.run_id = %s::uuid
                  AND findings.status = 'PENDING'::finding_status
            )
            """,
            (run_id,),
        ).fetchone()

        assert row is not None
        return cast(bool, row[0])
