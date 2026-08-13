from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import JsonValue

from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot

_RUN_RULE_SCOPE_COLUMNS = """
    run_id,
    captured_at,
    created_at
"""

_RUN_RULE_SET_SNAPSHOT_COLUMNS = """
    run_id,
    source_rule_set_id,
    name,
    description,
    version,
    created_at
"""

_RUN_RULE_SNAPSHOT_COLUMNS = """
    run_id,
    source_rule_id,
    source_rule_set_id,
    rule_key,
    title,
    description,
    severity,
    definition,
    created_at
"""


@dataclass(frozen=True)
class RunRuleScopeRecord:
    run_id: str
    captured_at: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunRuleScopeRecord":
        return cls(
            run_id=str(row[0]),
            captured_at=cast(datetime, row[1]),
            created_at=cast(datetime, row[2]),
        )


@dataclass(frozen=True)
class RunRuleSetSnapshotRecord:
    run_id: str
    source_rule_set_id: str
    name: str
    description: str | None
    version: int
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunRuleSetSnapshotRecord":
        return cls(
            run_id=str(row[0]),
            source_rule_set_id=str(row[1]),
            name=cast(str, row[2]),
            description=cast(str | None, row[3]),
            version=cast(int, row[4]),
            created_at=cast(datetime, row[5]),
        )

    def to_domain(self) -> RunRuleSetSnapshot:
        return RunRuleSetSnapshot(
            run_id=self.run_id,
            source_rule_set_id=self.source_rule_set_id,
            name=self.name,
            description=self.description,
            version=self.version,
        )


@dataclass(frozen=True)
class RunRuleSnapshotRecord:
    run_id: str
    source_rule_id: str
    source_rule_set_id: str
    rule_key: str
    title: str
    description: str | None
    severity: str
    definition: JsonValue
    created_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunRuleSnapshotRecord":
        return cls(
            run_id=str(row[0]),
            source_rule_id=str(row[1]),
            source_rule_set_id=str(row[2]),
            rule_key=cast(str, row[3]),
            title=cast(str, row[4]),
            description=cast(str | None, row[5]),
            severity=cast(str, row[6]),
            definition=cast(JsonValue, row[7]),
            created_at=cast(datetime, row[8]),
        )

    def to_domain(self) -> RunRuleSnapshot:
        return RunRuleSnapshot(
            run_id=self.run_id,
            source_rule_id=self.source_rule_id,
            source_rule_set_id=self.source_rule_set_id,
            rule_key=self.rule_key,
            title=self.title,
            description=self.description,
            severity=self.severity,
            definition=self.definition,
        )


class RunRuleScopeRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def insert_rule_set_snapshot(
        self,
        snapshot: RunRuleSetSnapshot,
    ) -> RunRuleSetSnapshotRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_rule_set_snapshots (
                run_id,
                source_rule_set_id,
                name,
                description,
                version
            )
            VALUES (%s::uuid, %s::uuid, %s, %s, %s)
            RETURNING {_RUN_RULE_SET_SNAPSHOT_COLUMNS}
            """,
            (
                snapshot.run_id,
                snapshot.source_rule_set_id,
                snapshot.name,
                snapshot.description,
                snapshot.version,
            ),
        ).fetchone()

        assert row is not None
        return RunRuleSetSnapshotRecord.from_row(row)

    def insert_rule_snapshot(self, snapshot: RunRuleSnapshot) -> RunRuleSnapshotRecord:
        definition = snapshot.model_dump(mode="json")["definition"]
        row = self._connection.execute(
            f"""
            INSERT INTO run_rule_snapshots (
                run_id,
                source_rule_id,
                source_rule_set_id,
                rule_key,
                title,
                description,
                severity,
                definition
            )
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s::jsonb)
            RETURNING {_RUN_RULE_SNAPSHOT_COLUMNS}
            """,
            (
                snapshot.run_id,
                snapshot.source_rule_id,
                snapshot.source_rule_set_id,
                snapshot.rule_key,
                snapshot.title,
                snapshot.description,
                snapshot.severity,
                Jsonb(definition),
            ),
        ).fetchone()

        assert row is not None
        return RunRuleSnapshotRecord.from_row(row)

    def complete_scope(self, run_id: str) -> RunRuleScopeRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO run_rule_scopes (run_id)
            VALUES (%s::uuid)
            RETURNING {_RUN_RULE_SCOPE_COLUMNS}
            """,
            (run_id,),
        ).fetchone()

        assert row is not None
        return RunRuleScopeRecord.from_row(row)

    def get_scope(self, run_id: str) -> RunRuleScopeRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_RULE_SCOPE_COLUMNS}
            FROM run_rule_scopes
            WHERE run_id = %s::uuid
            """,
            (run_id,),
        ).fetchone()

        return RunRuleScopeRecord.from_row(row) if row is not None else None

    def list_rule_sets_for_run(self, run_id: str) -> list[RunRuleSetSnapshotRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_RULE_SET_SNAPSHOT_COLUMNS}
            FROM run_rule_set_snapshots
            WHERE run_id = %s::uuid
            ORDER BY source_rule_set_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunRuleSetSnapshotRecord.from_row(row) for row in rows]

    def list_rules_for_run(self, run_id: str) -> list[RunRuleSnapshotRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_RULE_SNAPSHOT_COLUMNS}
            FROM run_rule_snapshots
            WHERE run_id = %s::uuid
            ORDER BY source_rule_id ASC
            """,
            (run_id,),
        ).fetchall()

        return [RunRuleSnapshotRecord.from_row(row) for row in rows]
