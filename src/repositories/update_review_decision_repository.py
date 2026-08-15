from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.update_review import UpdateReviewDecisionType

_UPDATE_REVIEW_DECISION_COLUMNS = """
    id,
    change_set_id,
    decision,
    actor_type,
    actor_id,
    idempotency_key,
    decided_at
"""


@dataclass(frozen=True)
class UpdateReviewDecisionRecord:
    update_review_decision_id: str
    change_set_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "UpdateReviewDecisionRecord":
        return cls(
            update_review_decision_id=str(row[0]),
            change_set_id=str(row[1]),
            decision=UpdateReviewDecisionType(str(row[2])),
            actor_type=cast(str, row[3]),
            actor_id=cast(str, row[4]),
            idempotency_key=cast(str, row[5]),
            decided_at=cast(datetime, row[6]),
        )


class UpdateReviewDecisionRepository:
    """Connection-scoped persistence for immutable UPDATE review decisions."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        change_set_id: str,
        decision: UpdateReviewDecisionType,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> UpdateReviewDecisionRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO update_review_decisions (
                change_set_id,
                decision,
                actor_type,
                actor_id,
                idempotency_key
            )
            VALUES (%s::uuid, %s::review_decision_type, %s, %s, %s)
            RETURNING {_UPDATE_REVIEW_DECISION_COLUMNS}
            """,
            (change_set_id, decision.value, actor_type, actor_id, idempotency_key),
        ).fetchone()
        assert row is not None
        return UpdateReviewDecisionRecord.from_row(row)

    def get_by_change_set_id(self, change_set_id: str) -> UpdateReviewDecisionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_REVIEW_DECISION_COLUMNS}
            FROM update_review_decisions
            WHERE change_set_id = %s::uuid
            """,
            (change_set_id,),
        ).fetchone()
        return UpdateReviewDecisionRecord.from_row(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> UpdateReviewDecisionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_REVIEW_DECISION_COLUMNS}
            FROM update_review_decisions
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        return UpdateReviewDecisionRecord.from_row(row) if row is not None else None
