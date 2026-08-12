from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import JsonValue

_AUDIT_EVENT_COLUMNS = """
    id,
    run_id,
    entity_type,
    entity_id,
    event_type,
    actor_type,
    actor_id,
    payload,
    created_at
"""


@dataclass(frozen=True)
class AuditEventRecord:
    audit_event_id: str
    run_id: str
    entity_type: str
    entity_id: str
    event_type: str
    actor_type: str
    actor_id: str
    payload: JsonValue
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "AuditEventRecord":
        return cls(
            audit_event_id=str(row[0]),
            run_id=str(row[1]),
            entity_type=cast(str, row[2]),
            entity_id=str(row[3]),
            event_type=cast(str, row[4]),
            actor_type=cast(str, row[5]),
            actor_id=cast(str, row[6]),
            payload=cast(JsonValue, row[7]),
            created_at=cast(datetime, row[8]),
        )


class AuditEventRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(
        self,
        run_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        payload: JsonValue,
    ) -> AuditEventRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO audit_events (
                run_id,
                entity_type,
                entity_id,
                event_type,
                actor_type,
                actor_id,
                payload
            )
            VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s::jsonb)
            RETURNING {_AUDIT_EVENT_COLUMNS}
            """,
            (
                run_id,
                entity_type,
                entity_id,
                event_type,
                actor_type,
                actor_id,
                Jsonb(payload),
            ),
        ).fetchone()

        assert row is not None
        return AuditEventRecord.from_row(row)

    def get_by_id(self, audit_event_id: str) -> AuditEventRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_AUDIT_EVENT_COLUMNS}
            FROM audit_events
            WHERE id = %s::uuid
            """,
            (audit_event_id,),
        ).fetchone()

        return AuditEventRecord.from_row(row) if row is not None else None

    def list_for_run(self, run_id: str) -> list[AuditEventRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_AUDIT_EVENT_COLUMNS}
            FROM audit_events
            WHERE run_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            """,
            (run_id,),
        ).fetchall()

        return [AuditEventRecord.from_row(row) for row in rows]
