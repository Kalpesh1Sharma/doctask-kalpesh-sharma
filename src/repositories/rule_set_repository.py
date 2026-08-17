"""Persistence for mutable user-supplied RuleSet configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import JsonValue

_RULE_SET_COLUMNS = "id, pile_id, name, description, version, created_at"
_RULE_COLUMNS = "id, rule_set_id, rule_key, title, description, severity, definition, created_at"


@dataclass(frozen=True)
class RuleSetRecord:
    rule_set_id: str
    pile_id: str
    name: str
    description: str | None
    version: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> RuleSetRecord:
        return cls(
            rule_set_id=str(row[0]),
            pile_id=str(row[1]),
            name=cast(str, row[2]),
            description=cast(str | None, row[3]),
            version=cast(int, row[4]),
            created_at=cast(datetime, row[5]),
        )


@dataclass(frozen=True)
class RuleRecord:
    rule_id: str
    rule_set_id: str
    rule_key: str
    title: str
    description: str | None
    severity: str
    definition: JsonValue
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> RuleRecord:
        return cls(
            rule_id=str(row[0]),
            rule_set_id=str(row[1]),
            rule_key=cast(str, row[2]),
            title=cast(str, row[3]),
            description=cast(str | None, row[4]),
            severity=cast(str, row[5]),
            definition=cast(JsonValue, row[6]),
            created_at=cast(datetime, row[7]),
        )


class RuleSetRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create_rule_set(
        self,
        pile_id: str,
        name: str,
        description: str | None,
        version: int,
    ) -> RuleSetRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO rule_sets (pile_id, name, description, version)
            VALUES (%s::uuid, %s, %s, %s)
            RETURNING {_RULE_SET_COLUMNS}
            """,
            (pile_id, name, description, version),
        ).fetchone()
        assert row is not None
        return RuleSetRecord.from_row(row)

    def create_rule(
        self,
        rule_set_id: str,
        *,
        rule_key: str,
        title: str,
        description: str | None,
        severity: str,
        definition: JsonValue,
    ) -> RuleRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO rules (
                rule_set_id, rule_key, title, description, severity, definition
            )
            VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb)
            RETURNING {_RULE_COLUMNS}
            """,
            (
                rule_set_id,
                rule_key,
                title,
                description,
                severity,
                Jsonb(definition),
            ),
        ).fetchone()
        assert row is not None
        return RuleRecord.from_row(row)

    def get_rule_set(self, rule_set_id: str) -> RuleSetRecord | None:
        row = self._connection.execute(
            f"SELECT {_RULE_SET_COLUMNS} FROM rule_sets WHERE id = %s::uuid",
            (rule_set_id,),
        ).fetchone()
        return RuleSetRecord.from_row(row) if row is not None else None

    def list_rule_sets_for_pile(self, pile_id: str, *, limit: int) -> list[RuleSetRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RULE_SET_COLUMNS}
            FROM rule_sets
            WHERE pile_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (pile_id, limit),
        ).fetchall()
        return [RuleSetRecord.from_row(row) for row in rows]

    def list_rules_for_rule_set(self, rule_set_id: str) -> list[RuleRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RULE_COLUMNS}
            FROM rules
            WHERE rule_set_id = %s::uuid
            ORDER BY rule_key ASC, id ASC
            """,
            (rule_set_id,),
        ).fetchall()
        return [RuleRecord.from_row(row) for row in rows]
