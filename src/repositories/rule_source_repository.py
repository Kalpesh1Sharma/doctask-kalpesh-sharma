from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from psycopg import Connection
from pydantic import JsonValue


@dataclass(frozen=True)
class RuleSetSourceRecord:
    rule_set_id: str
    pile_id: str
    name: str
    description: str | None
    version: int

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RuleSetSourceRecord":
        return cls(
            rule_set_id=str(row[0]),
            pile_id=str(row[1]),
            name=cast(str, row[2]),
            description=cast(str | None, row[3]),
            version=cast(int, row[4]),
        )


@dataclass(frozen=True)
class RuleSourceRecord:
    rule_id: str
    rule_set_id: str
    pile_id: str
    rule_key: str
    title: str
    description: str | None
    severity: str
    definition: JsonValue

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RuleSourceRecord":
        return cls(
            rule_id=str(row[0]),
            rule_set_id=str(row[1]),
            pile_id=str(row[2]),
            rule_key=cast(str, row[3]),
            title=cast(str, row[4]),
            description=cast(str | None, row[5]),
            severity=cast(str, row[6]),
            definition=cast(JsonValue, row[7]),
        )


class RuleSourceRepository:
    """Read the mutable rule configuration under caller-owned shared locks."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def load_rule_sets_for_share(
        self,
        rule_set_ids: Sequence[str],
    ) -> list[RuleSetSourceRecord]:
        if not rule_set_ids:
            return []

        rows = self._connection.execute(
            """
            SELECT id, pile_id, name, description, version
            FROM rule_sets
            WHERE id = ANY(%s::uuid[])
            ORDER BY id ASC
            FOR SHARE
            """,
            (list(rule_set_ids),),
        ).fetchall()

        return [RuleSetSourceRecord.from_row(row) for row in rows]

    def load_rules_for_share(self, rule_ids: Sequence[str]) -> list[RuleSourceRecord]:
        if not rule_ids:
            return []

        rows = self._connection.execute(
            """
            SELECT
                rules.id,
                rules.rule_set_id,
                rule_sets.pile_id,
                rules.rule_key,
                rules.title,
                rules.description,
                rules.severity,
                rules.definition
            FROM rules
            JOIN rule_sets ON rule_sets.id = rules.rule_set_id
            WHERE rules.id = ANY(%s::uuid[])
            ORDER BY rules.id ASC
            FOR SHARE OF rules, rule_sets
            """,
            (list(rule_ids),),
        ).fetchall()

        return [RuleSourceRecord.from_row(row) for row in rows]
