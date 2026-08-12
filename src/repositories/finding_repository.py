from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.finding import Finding, FindingStatus, FindingType

_FINDING_COLUMNS = """
    id,
    origin_run_id,
    rule_id,
    finding_type,
    status,
    title,
    description,
    severity,
    created_at,
    updated_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class FindingRecord:
    finding_id: str
    origin_run_id: str
    rule_id: str | None
    finding_type: FindingType
    status: FindingStatus
    title: str
    description: str | None
    severity: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "FindingRecord":
        return cls(
            finding_id=str(row[0]),
            origin_run_id=str(row[1]),
            rule_id=_optional_string(row[2]),
            finding_type=FindingType(str(row[3])),
            status=FindingStatus(str(row[4])),
            title=cast(str, row[5]),
            description=_optional_string(row[6]),
            severity=cast(str, row[7]),
            created_at=cast(datetime, row[8]),
            updated_at=cast(datetime, row[9]),
        )

    def to_domain(self) -> Finding:
        return Finding(
            finding_id=self.finding_id,
            origin_run_id=self.origin_run_id,
            finding_type=self.finding_type,
            rule_id=self.rule_id,
            title=self.title,
            description=self.description,
            severity=self.severity,
            status=self.status,
        )


class FindingRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, finding: Finding) -> FindingRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO findings (
                id,
                origin_run_id,
                rule_id,
                finding_type,
                status,
                title,
                description,
                severity
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s::uuid,
                %s::finding_type,
                %s::finding_status,
                %s,
                %s,
                %s
            )
            RETURNING {_FINDING_COLUMNS}
            """,
            (
                finding.finding_id,
                finding.origin_run_id,
                finding.rule_id,
                finding.finding_type.value,
                finding.status.value,
                finding.title,
                finding.description,
                finding.severity,
            ),
        ).fetchone()

        assert row is not None
        return FindingRecord.from_row(row)

    def get_by_id(self, finding_id: str) -> FindingRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_FINDING_COLUMNS}
            FROM findings
            WHERE id = %s::uuid
            """,
            (finding_id,),
        ).fetchone()

        return FindingRecord.from_row(row) if row is not None else None

    def list_for_origin_run(self, run_id: str) -> list[FindingRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_FINDING_COLUMNS}
            FROM findings
            WHERE origin_run_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()

        return [FindingRecord.from_row(row) for row in rows]

    def update_status(
        self,
        finding_id: str,
        status: FindingStatus,
    ) -> FindingRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE findings
            SET status = %s::finding_status
            WHERE id = %s::uuid
            RETURNING {_FINDING_COLUMNS}
            """,
            (status.value, finding_id),
        ).fetchone()

        return FindingRecord.from_row(row) if row is not None else None
