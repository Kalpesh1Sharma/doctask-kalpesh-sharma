from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.review_decision import ReviewDecisionType

_REVIEW_DECISION_COLUMNS = """
    id,
    finding_id,
    decision,
    actor_type,
    actor_id,
    created_at,
    idempotency_key
"""


@dataclass(frozen=True)
class ReviewDecisionRecord:
    review_decision_id: str
    finding_id: str
    decision: ReviewDecisionType
    actor_type: str
    actor_id: str
    created_at: datetime
    idempotency_key: str

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "ReviewDecisionRecord":
        return cls(
            review_decision_id=str(row[0]),
            finding_id=str(row[1]),
            decision=ReviewDecisionType(str(row[2])),
            actor_type=cast(str, row[3]),
            actor_id=cast(str, row[4]),
            created_at=cast(datetime, row[5]),
            idempotency_key=cast(str, row[6]),
        )


class ReviewDecisionRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        finding_id: str,
        decision: ReviewDecisionType,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> ReviewDecisionRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO review_decisions (
                finding_id,
                decision,
                actor_type,
                actor_id,
                idempotency_key
            )
            VALUES (%s::uuid, %s::review_decision_type, %s, %s, %s)
            RETURNING {_REVIEW_DECISION_COLUMNS}
            """,
            (finding_id, decision.value, actor_type, actor_id, idempotency_key),
        ).fetchone()

        assert row is not None
        return ReviewDecisionRecord.from_row(row)

    def get_by_id(self, review_decision_id: str) -> ReviewDecisionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_REVIEW_DECISION_COLUMNS}
            FROM review_decisions
            WHERE id = %s::uuid
            """,
            (review_decision_id,),
        ).fetchone()

        return ReviewDecisionRecord.from_row(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> ReviewDecisionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_REVIEW_DECISION_COLUMNS}
            FROM review_decisions
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()

        return ReviewDecisionRecord.from_row(row) if row is not None else None

    def list_for_finding(self, finding_id: str) -> list[ReviewDecisionRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_REVIEW_DECISION_COLUMNS}
            FROM review_decisions
            WHERE finding_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (finding_id,),
        ).fetchall()

        return [ReviewDecisionRecord.from_row(row) for row in rows]
