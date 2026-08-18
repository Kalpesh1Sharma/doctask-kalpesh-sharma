from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.update_review import UpdateReviewDecisionType

_UPDATE_ITEM_REVIEW_DECISION_COLUMNS = """
    id,
    item_id,
    decision,
    actor_type,
    actor_id,
    idempotency_key,
    decided_at
"""


@dataclass(frozen=True)
class UpdateItemReviewDecisionRecord:
    update_item_review_decision_id: str
    item_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "UpdateItemReviewDecisionRecord":
        return cls(
            update_item_review_decision_id=str(row[0]),
            item_id=str(row[1]),
            decision=UpdateReviewDecisionType(str(row[2])),
            actor_type=cast(str, row[3]),
            actor_id=cast(str, row[4]),
            idempotency_key=cast(str, row[5]),
            decided_at=cast(datetime, row[6]),
        )


class UpdateItemReviewDecisionRepository:
    """Connection-scoped persistence for immutable item-level UPDATE decisions."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        item_id: str,
        decision: UpdateReviewDecisionType,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> UpdateItemReviewDecisionRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO update_item_review_decisions (
                item_id,
                decision,
                actor_type,
                actor_id,
                idempotency_key
            )
            VALUES (%s::uuid, %s::review_decision_type, %s, %s, %s)
            RETURNING {_UPDATE_ITEM_REVIEW_DECISION_COLUMNS}
            """,
            (item_id, decision.value, actor_type, actor_id, idempotency_key),
        ).fetchone()
        assert row is not None
        return UpdateItemReviewDecisionRecord.from_row(row)

    def get_by_item_id(self, item_id: str) -> UpdateItemReviewDecisionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_ITEM_REVIEW_DECISION_COLUMNS}
            FROM update_item_review_decisions
            WHERE item_id = %s::uuid
            """,
            (item_id,),
        ).fetchone()
        return UpdateItemReviewDecisionRecord.from_row(row) if row is not None else None

    def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> UpdateItemReviewDecisionRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_ITEM_REVIEW_DECISION_COLUMNS}
            FROM update_item_review_decisions
            WHERE idempotency_key = %s
            """,
            (idempotency_key,),
        ).fetchone()
        return UpdateItemReviewDecisionRecord.from_row(row) if row is not None else None

    def list_for_change_set(self, change_set_id: str) -> list[UpdateItemReviewDecisionRecord]:
        rows = self._connection.execute(
            """
            SELECT
                decisions.id,
                decisions.item_id,
                decisions.decision,
                decisions.actor_type,
                decisions.actor_id,
                decisions.idempotency_key,
                decisions.decided_at
            FROM update_item_review_decisions AS decisions
            JOIN update_change_items AS items ON items.id = decisions.item_id
            WHERE items.change_set_id = %s::uuid
            ORDER BY items.fact_id ASC
            """,
            (change_set_id,),
        ).fetchall()
        return [UpdateItemReviewDecisionRecord.from_row(row) for row in rows]
