from dataclasses import dataclass
from datetime import datetime
from typing import cast

from psycopg import Connection

from domain.run import Run, RunStatus, RunType

_RUN_COLUMNS = """
    id,
    pile_id,
    run_type,
    status,
    trigger_document_version_id,
    parent_run_id,
    baseline_run_id,
    blocked_on_run_id,
    commit_required,
    started_at,
    completed_at,
    created_at,
    updated_at
"""


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    pile_id: str
    run_type: RunType
    status: RunStatus
    trigger_document_version_id: str | None
    parent_run_id: str | None
    baseline_run_id: str | None
    blocked_on_run_id: str | None
    commit_required: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: tuple[object, ...]) -> "RunRecord":
        return cls(
            run_id=str(row[0]),
            pile_id=str(row[1]),
            run_type=RunType(str(row[2])),
            status=RunStatus(str(row[3])),
            trigger_document_version_id=_optional_string(row[4]),
            parent_run_id=_optional_string(row[5]),
            baseline_run_id=_optional_string(row[6]),
            blocked_on_run_id=_optional_string(row[7]),
            commit_required=cast(bool, row[8]),
            started_at=cast(datetime | None, row[9]),
            completed_at=cast(datetime | None, row[10]),
            created_at=cast(datetime, row[11]),
            updated_at=cast(datetime, row[12]),
        )

    def to_domain(self) -> Run:
        return Run(
            run_id=self.run_id,
            pile_id=self.pile_id,
            run_type=self.run_type,
            status=self.status,
            trigger_document_version_id=self.trigger_document_version_id,
            parent_run_id=self.parent_run_id,
            baseline_run_id=self.baseline_run_id,
            blocked_on_run_id=self.blocked_on_run_id,
        )


class RunRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create(self, run: Run) -> RunRecord:
        row = self._connection.execute(
            f"""
            INSERT INTO runs (
                id,
                pile_id,
                run_type,
                status,
                trigger_document_version_id,
                parent_run_id,
                baseline_run_id,
                blocked_on_run_id
            )
            VALUES (
                %s::uuid,
                %s::uuid,
                %s::run_type,
                %s::run_status,
                %s::uuid,
                %s::uuid,
                %s::uuid,
                %s::uuid
            )
            RETURNING {_RUN_COLUMNS}
            """,
            (
                run.run_id,
                run.pile_id,
                run.run_type.value,
                run.status.value,
                run.trigger_document_version_id,
                run.parent_run_id,
                run.baseline_run_id,
                run.blocked_on_run_id,
            ),
        ).fetchone()

        assert row is not None
        return RunRecord.from_row(row)

    def get_by_id(self, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE id = %s::uuid
            """,
            (run_id,),
        ).fetchone()

        return RunRecord.from_row(row) if row is not None else None

    def get_for_update(self, run_id: str) -> RunRecord | None:
        row = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE id = %s::uuid
            FOR UPDATE
            """,
            (run_id,),
        ).fetchone()

        return RunRecord.from_row(row) if row is not None else None

    def update_status(self, run_id: str, status: RunStatus) -> RunRecord | None:
        row = self._connection.execute(
            f"""
            UPDATE runs
            SET
                status = %s::run_status,
                completed_at = CASE
                    WHEN %s::run_status = 'COMPLETED'
                    THEN COALESCE(completed_at, now())
                    ELSE completed_at
                END
            WHERE id = %s::uuid
            RETURNING {_RUN_COLUMNS}
            """,
            (status.value, status.value, run_id),
        ).fetchone()

        return RunRecord.from_row(row) if row is not None else None

    def resolve_blocked_update_baseline(
        self,
        run_id: str,
        baseline_run_id: str,
    ) -> RunRecord | None:
        """Resolve a completed parent without changing the durable pile tail."""
        row = self._connection.execute(
            f"""
            UPDATE runs
            SET baseline_run_id = %s::uuid, blocked_on_run_id = NULL
            WHERE id = %s::uuid
              AND run_type = 'UPDATE'::run_type
              AND blocked_on_run_id = %s::uuid
            RETURNING {_RUN_COLUMNS}
            """,
            (baseline_run_id, run_id, baseline_run_id),
        ).fetchone()
        return RunRecord.from_row(row) if row is not None else None

    def list_worker_candidates(self, *, limit: int) -> list[RunRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE run_type IN ('INITIAL'::run_type, 'UPDATE'::run_type)
              AND status IN (
                'PENDING'::run_status,
                'RUNNING'::run_status,
                'REVIEW_REQUIRED'::run_status
            )
              AND NOT EXISTS (
                SELECT 1
                FROM run_steps
                WHERE run_steps.run_id = runs.id
                  AND run_steps.status IN (
                    'ESCALATED'::run_step_status,
                    'FAILED'::run_step_status
                  )
              )
              AND (
                status <> 'REVIEW_REQUIRED'::run_status
                OR (
                  -- A run waiting on a human decision is not executable work.
                  -- Do not let historical REVIEW_REQUIRED rows consume the
                  -- worker batch until their finding/update review is complete.
                  NOT EXISTS (
                    SELECT 1
                    FROM run_findings
                    JOIN findings ON findings.id = run_findings.finding_id
                    WHERE run_findings.run_id = runs.id
                      AND findings.status = 'PENDING'::finding_status
                  )
                  AND (
                    (
                      run_type = 'INITIAL'::run_type
                      AND (
                        EXISTS (
                          SELECT 1
                          FROM run_findings
                          WHERE run_findings.run_id = runs.id
                        )
                        OR EXISTS (
                          SELECT 1
                          FROM audit_events
                          WHERE audit_events.run_id = runs.id
                            AND audit_events.entity_type = 'run'
                            AND audit_events.entity_id = runs.id
                            AND audit_events.event_type = 'RUN_REVIEW_ACKNOWLEDGED'
                        )
                      )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM update_change_sets
                      WHERE update_change_sets.run_id = runs.id
                        AND (
                          (
                            EXISTS (
                              SELECT 1
                              FROM update_change_items
                              WHERE update_change_items.change_set_id = update_change_sets.id
                            )
                            AND NOT EXISTS (
                              SELECT 1
                              FROM update_change_items
                              LEFT JOIN update_item_review_decisions
                                ON update_item_review_decisions.item_id = update_change_items.id
                              WHERE update_change_items.change_set_id = update_change_sets.id
                                AND update_item_review_decisions.id IS NULL
                            )
                            AND NOT EXISTS (
                              SELECT 1
                              FROM update_review_decisions
                              WHERE update_review_decisions.change_set_id = update_change_sets.id
                                AND update_review_decisions.decision = 'REJECTED'::review_decision_type
                            )
                          )
                          OR (
                            NOT EXISTS (
                              SELECT 1
                              FROM update_change_items
                              WHERE update_change_items.change_set_id = update_change_sets.id
                            )
                            AND EXISTS (
                              SELECT 1
                              FROM update_review_decisions
                              WHERE update_review_decisions.change_set_id = update_change_sets.id
                                AND update_review_decisions.decision = 'APPROVED'::review_decision_type
                            )
                          )
                        )
                    )
                  )
                )
              )
            ORDER BY
                ROW_NUMBER() OVER (
                    PARTITION BY status
                    ORDER BY created_at ASC, id ASC
                ) ASC,
                CASE status
                    WHEN 'REVIEW_REQUIRED'::run_status THEN 0
                    WHEN 'PENDING'::run_status THEN 1
                    WHEN 'RUNNING'::run_status THEN 2
                    ELSE 3
                END ASC,
                created_at ASC,
                id ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def list_completed_updates_for_pile(
        self,
        pile_id: str,
        *,
        limit: int,
    ) -> list[RunRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE pile_id = %s::uuid
              AND run_type = 'UPDATE'::run_type
              AND status = 'COMPLETED'::run_status
            ORDER BY completed_at DESC, id DESC
            LIMIT %s
            """,
            (pile_id, limit),
        ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def list_for_pile_limited(self, pile_id: str, *, limit: int) -> list[RunRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE pile_id = %s::uuid
            ORDER BY created_at ASC, id ASC
            LIMIT %s
            """,
            (pile_id, limit),
        ).fetchall()
        return [RunRecord.from_row(row) for row in rows]

    def list_for_pile(self, pile_id: str) -> list[RunRecord]:
        rows = self._connection.execute(
            f"""
            SELECT {_RUN_COLUMNS}
            FROM runs
            WHERE pile_id = %s::uuid
            ORDER BY id ASC
            """,
            (pile_id,),
        ).fetchall()
        return [RunRecord.from_row(row) for row in rows]
