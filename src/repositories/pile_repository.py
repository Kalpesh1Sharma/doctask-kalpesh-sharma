"""Persistence-only access to durable pile lineage tails."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

_PILE_COLUMNS = "id, name, description, watch_enabled, watch_location, latest_run_id, created_at, updated_at"


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class PileRecord:
    pile_id: str
    name: str
    description: str | None
    watch_enabled: bool
    watch_location: str | None
    latest_run_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> PileRecord:
        return cls(
            pile_id=str(row[0]),
            name=cast(str, row[1]),
            description=_optional_string(row[2]),
            watch_enabled=cast(bool, row[3]),
            watch_location=_optional_string(row[4]),
            latest_run_id=_optional_string(row[5]),
            created_at=cast(datetime, row[6]),
            updated_at=cast(datetime, row[7]),
        )


class PileRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def get_by_id(self, pile_id: str) -> PileRecord | None:
        row = self._connection.execute(
            f"SELECT {_PILE_COLUMNS} FROM piles WHERE id = %s::uuid",
            (pile_id,),
        ).fetchone()
        return PileRecord.from_row(row) if row is not None else None

    def get_for_update(self, pile_id: str) -> PileRecord | None:
        row = self._connection.execute(
            f"SELECT {_PILE_COLUMNS} FROM piles WHERE id = %s::uuid FOR UPDATE",
            (pile_id,),
        ).fetchone()
        return PileRecord.from_row(row) if row is not None else None

    def list_ids_with_ready_current_versions(self, limit: int) -> tuple[str, ...]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT piles.id
            FROM piles
            JOIN documents ON documents.pile_id = piles.id
            JOIN document_versions ON document_versions.id = documents.current_version_id
            WHERE document_versions.processing_status = 'READY'::document_version_status
            ORDER BY piles.id ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def set_latest_run(self, pile_id: str, run_id: str) -> PileRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE piles
            SET latest_run_id = %s::uuid
            WHERE id = %s::uuid
            RETURNING {_PILE_COLUMNS}
            """,
            (run_id, pile_id),
        ).fetchone()
        return PileRecord.from_row(row) if row is not None else None
