from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import cast

from psycopg import Connection


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InProgressClaimState(StrEnum):
    MATCHING_FRESH = "MATCHING_FRESH"
    MATCHING_STALE = "MATCHING_STALE"
    CHANGED_OR_MISSING = "CHANGED_OR_MISSING"


_IDEMPOTENCY_COLUMNS = """
    id,
    scope,
    key,
    status,
    result_reference,
    claimed_at,
    claim_generation,
    created_at,
    completed_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class IdempotencyRecord:
    record_id: str
    scope: str
    key: str
    status: IdempotencyStatus
    result_reference: str | None
    claimed_at: datetime | None
    claim_generation: int
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "IdempotencyRecord":
        return cls(
            record_id=str(row[0]),
            scope=cast(str, row[1]),
            key=cast(str, row[2]),
            status=IdempotencyStatus(str(row[3])),
            result_reference=_optional_string(row[4]),
            claimed_at=cast(datetime | None, row[5]),
            claim_generation=int(row[6]),
            created_at=cast(datetime, row[7]),
            completed_at=cast(datetime | None, row[8]),
        )


class IdempotencyRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def claim(self, scope: str, key: str) -> IdempotencyRecord | None:
        row = self._connection.execute(
            f"""
            INSERT INTO idempotency_records (
                scope,
                key,
                status,
                claimed_at,
                claim_generation
            )
            VALUES (
                %s,
                %s,
                'IN_PROGRESS'::idempotency_status,
                clock_timestamp(),
                1
            )
            ON CONFLICT (scope, key) DO NOTHING
            RETURNING {_IDEMPOTENCY_COLUMNS}
            """,
            (scope, key),
        ).fetchone()

        return IdempotencyRecord.from_row(row) if row is not None else None

    def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_IDEMPOTENCY_COLUMNS}
            FROM idempotency_records
            WHERE scope = %s
              AND key = %s
            """,
            (scope, key),
        ).fetchone()

        return IdempotencyRecord.from_row(row) if row is not None else None

    def classify_in_progress(
        self,
        scope: str,
        key: str,
        observed_generation: int,
        stale_after: timedelta,
    ) -> InProgressClaimState:
        row = self._connection.execute(
            """
            SELECT CASE
                WHEN status = 'IN_PROGRESS'::idempotency_status
                 AND claim_generation = %s
                 AND claimed_at <= clock_timestamp() - %s::interval
                    THEN 'MATCHING_STALE'
                WHEN status = 'IN_PROGRESS'::idempotency_status
                 AND claim_generation = %s
                    THEN 'MATCHING_FRESH'
                ELSE 'CHANGED_OR_MISSING'
            END
            FROM idempotency_records
            WHERE scope = %s AND key = %s
            """,
            (observed_generation, stale_after, observed_generation, scope, key),
        ).fetchone()
        return (
            InProgressClaimState(str(row[0]))
            if row is not None
            else InProgressClaimState.CHANGED_OR_MISSING
        )

    def reclaim_stale(
        self,
        scope: str,
        key: str,
        observed_generation: int,
        stale_after: timedelta,
    ) -> IdempotencyRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE idempotency_records
            SET
                claim_generation = claim_generation + 1,
                claimed_at = clock_timestamp()
            WHERE scope = %s
              AND key = %s
              AND status = 'IN_PROGRESS'::idempotency_status
              AND claim_generation = %s
              AND claimed_at <= clock_timestamp() - %s::interval
            RETURNING {_IDEMPOTENCY_COLUMNS}
            """,
            (scope, key, observed_generation, stale_after),
        ).fetchone()

        return IdempotencyRecord.from_row(row) if row is not None else None

    def mark_completed(
        self,
        scope: str,
        key: str,
        owner_generation: int,
        result_reference: str | None,
    ) -> IdempotencyRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE idempotency_records
            SET
                status = 'COMPLETED'::idempotency_status,
                result_reference = %s,
                completed_at = now()
            WHERE scope = %s
              AND key = %s
              AND status = 'IN_PROGRESS'::idempotency_status
              AND claim_generation = %s
            RETURNING {_IDEMPOTENCY_COLUMNS}
            """,
            (result_reference, scope, key, owner_generation),
        ).fetchone()

        return IdempotencyRecord.from_row(row) if row is not None else None

    def mark_failed(
        self,
        scope: str,
        key: str,
        owner_generation: int,
    ) -> IdempotencyRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE idempotency_records
            SET status = 'FAILED'::idempotency_status
            WHERE scope = %s
              AND key = %s
              AND status = 'IN_PROGRESS'::idempotency_status
              AND claim_generation = %s
            RETURNING {_IDEMPOTENCY_COLUMNS}
            """,
            (scope, key, owner_generation),
        ).fetchone()

        return IdempotencyRecord.from_row(row) if row is not None else None
