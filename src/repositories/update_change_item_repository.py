from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from pydantic import JsonValue

from domain.update_review import UpdateChangeItemKind

_UPDATE_CHANGE_ITEM_COLUMNS = """
    id,
    change_set_id,
    fact_id,
    kind,
    payload,
    created_at
"""


@dataclass(frozen=True)
class UpdateChangeItemRecord:
    item_id: str
    change_set_id: str
    fact_id: str
    kind: UpdateChangeItemKind
    payload: JsonValue
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "UpdateChangeItemRecord":
        return cls(
            item_id=str(row[0]),
            change_set_id=str(row[1]),
            fact_id=str(row[2]),
            kind=UpdateChangeItemKind(str(row[3])),
            payload=cast(JsonValue, row[4]),
            created_at=cast(datetime, row[5]),
        )


class UpdateChangeItemRepository:
    """Connection-scoped reads for immutable material UPDATE review items."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def list_for_change_set(self, change_set_id: str) -> list[UpdateChangeItemRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_UPDATE_CHANGE_ITEM_COLUMNS}
            FROM update_change_items
            WHERE change_set_id = %s::uuid
            ORDER BY fact_id ASC
            """,
            (change_set_id,),
        ).fetchall()
        return [UpdateChangeItemRecord.from_row(row) for row in rows]

    def get_by_change_set_and_fact_id(
        self,
        change_set_id: str,
        fact_id: str,
    ) -> UpdateChangeItemRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_UPDATE_CHANGE_ITEM_COLUMNS}
            FROM update_change_items
            WHERE change_set_id = %s::uuid
              AND fact_id = %s::uuid
            """,
            (change_set_id, fact_id),
        ).fetchone()
        return UpdateChangeItemRecord.from_row(row) if row is not None else None
