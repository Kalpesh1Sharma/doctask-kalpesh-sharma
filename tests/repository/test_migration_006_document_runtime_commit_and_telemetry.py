from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException

from config.settings import get_settings

_MIGRATIONS_001_TO_005 = tuple(
    Path(f"sql/migrations/{number:03d}_{name}.sql")
    for number, name in (
        (1, "schema_v2_7"),
        (2, "run_rule_scope"),
        (3, "update_review"),
        (4, "idempotency_claim_generation"),
        (5, "run_step_retry_history"),
    )
)
_MIGRATION_006 = Path("sql/migrations/006_document_runtime_commit_and_telemetry.sql")


@dataclass(frozen=True)
class Migration006Database:
    conninfo: str
    historical_completed_run_id: str
    historical_pending_run_id: str
    historical_running_run_id: str
    historical_active_run_id: str
    pile_id: str
    ready_version_id: str
    failed_version_id: str


def _fingerprint(character: str) -> str:
    return f"sha256:{character * 64}"


def _insert_closed_step_with_metric(connection, run_id: str, stage: str) -> None:
    connection.execute(
        """
        INSERT INTO run_steps (
            run_id, stage, status, attempt, started_at, completed_at
        ) VALUES (
            %s::uuid, %s::run_stage, 'COMPLETED'::run_step_status, 1,
            clock_timestamp() - interval '1 second', clock_timestamp()
        )
        """,
        (run_id, stage),
    )
    connection.execute(
        """
        INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
        SELECT run_id, stage, attempt,
               FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT
        FROM run_steps
        WHERE run_id = %s::uuid AND stage = %s::run_stage AND attempt = 1
        """,
        (run_id, stage),
    )


def _seed_pre_006_history(connection) -> dict[str, str]:
    pile = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"migration-006-history-{uuid4()}",),
    ).fetchone()
    assert pile is not None
    pile_id = str(pile[0])

    historical_completed = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, completed_at)
        VALUES (%s::uuid, 'MANUAL'::run_type, 'COMPLETED'::run_status, clock_timestamp())
        RETURNING id
        """,
        (pile_id,),
    ).fetchone()
    historical_pending = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
        RETURNING id
        """,
        (pile_id,),
    ).fetchone()
    historical_running = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, 'RUNNING'::run_status)
        RETURNING id
        """,
        (pile_id,),
    ).fetchone()
    historical_active = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, 'REVIEW_REQUIRED'::run_status)
        RETURNING id
        """,
        (pile_id,),
    ).fetchone()
    assert historical_completed is not None
    assert historical_pending is not None
    assert historical_running is not None
    assert historical_active is not None

    document = connection.execute(
        """
        INSERT INTO documents (pile_id, logical_key)
        VALUES (%s::uuid, %s)
        RETURNING id
        """,
        (pile_id, f"migration-006-document-{uuid4()}"),
    ).fetchone()
    assert document is not None
    ready_version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        ) VALUES (%s::uuid, 1, %s, %s, 'READY'::document_version_status)
        RETURNING id
        """,
        (str(document[0]), _fingerprint("a"), f"source://ready/{uuid4()}"),
    ).fetchone()
    failed_version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        ) VALUES (%s::uuid, 2, %s, %s, 'FAILED'::document_version_status)
        RETURNING id
        """,
        (str(document[0]), _fingerprint("b"), f"source://failed/{uuid4()}"),
    ).fetchone()
    assert ready_version is not None and failed_version is not None

    historical_active_run_id = str(historical_active[0])
    for stage in ("INGEST", "UNDERSTAND", "EXAMINE", "REVIEW"):
        _insert_closed_step_with_metric(connection, historical_active_run_id, stage)

    return {
        "pile_id": pile_id,
        "historical_completed_run_id": str(historical_completed[0]),
        "historical_pending_run_id": str(historical_pending[0]),
        "historical_running_run_id": str(historical_running[0]),
        "historical_active_run_id": historical_active_run_id,
        "ready_version_id": str(ready_version[0]),
        "failed_version_id": str(failed_version[0]),
    }


@pytest.fixture(scope="module")
def migration_006_database() -> Migration006Database:
    database_name = f"task1_phase6a_migration006_{uuid4().hex}"
    connection_info = conninfo_to_dict(get_settings().database_url)
    connection_info["dbname"] = "postgres"
    admin_info = make_conninfo(**connection_info)

    with connect(admin_info, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')

    database_info = dict(connection_info)
    database_info["dbname"] = database_name
    database_conninfo = make_conninfo(**database_info)
    try:
        with connect(database_conninfo) as isolated:
            for migration in _MIGRATIONS_001_TO_005:
                isolated.execute(migration.read_text(encoding="utf-8"))
                isolated.commit()
            seeded = _seed_pre_006_history(isolated)
            isolated.commit()
            isolated.execute(_MIGRATION_006.read_text(encoding="utf-8"))
            isolated.commit()

        yield Migration006Database(conninfo=database_conninfo, **seeded)
    finally:
        with connect(admin_info, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


def _create_review_required_run(connection, pile_id: str) -> str:
    row = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, 'REVIEW_REQUIRED'::run_status)
        RETURNING id
        """,
        (pile_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _start_commit_step(connection, run_id: str) -> None:
    connection.execute(
        """
        INSERT INTO run_steps (run_id, stage, status, attempt)
        VALUES (%s::uuid, 'COMMIT'::run_stage, 'PENDING'::run_step_status, 1)
        """,
        (run_id,),
    )
    connection.execute(
        """
        UPDATE run_steps
        SET status = 'RUNNING'::run_step_status, started_at = clock_timestamp()
        WHERE run_id = %s::uuid AND stage = 'COMMIT'::run_stage AND attempt = 1
        """,
        (run_id,),
    )


def _insert_final_report(connection, run_id: str, marker: str) -> None:
    connection.execute(
        """
        INSERT INTO outputs (run_id, output_type, storage_uri, content_hash)
        VALUES (%s::uuid, 'FINAL_REPORT', %s, %s)
        """,
        (run_id, f"artifact://migration-006/{marker}", _fingerprint(marker)),
    )


def test_migration_006_cutover_is_truthful_and_schema_stays_27_tables(
    migration_006_database: Migration006Database,
) -> None:
    with connect(migration_006_database.conninfo) as connection:
        historical_run_ids = (
            migration_006_database.historical_completed_run_id,
            migration_006_database.historical_pending_run_id,
            migration_006_database.historical_running_run_id,
            migration_006_database.historical_active_run_id,
        )
        cutover_rows = connection.execute(
            """
            SELECT id::text, commit_required
            FROM runs
            WHERE id IN (%s::uuid, %s::uuid, %s::uuid, %s::uuid)
            """,
            historical_run_ids,
        ).fetchall()
        cutover = {str(row[0]): bool(row[1]) for row in cutover_rows}
        assert cutover == {
            migration_006_database.historical_completed_run_id: False,
            migration_006_database.historical_pending_run_id: True,
            migration_006_database.historical_running_run_id: True,
            migration_006_database.historical_active_run_id: True,
        }
        assert connection.execute(
            """
            SELECT count(*)
            FROM run_steps
            WHERE run_id IN (%s::uuid, %s::uuid, %s::uuid, %s::uuid)
              AND stage = 'COMMIT'::run_stage
            """,
            historical_run_ids,
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM provider_usage_events").fetchone() == (0,)
        assert connection.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """,
        ).fetchone() == (27,)

        new_run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
            RETURNING commit_required
            """,
            (migration_006_database.pile_id,),
        ).fetchone()
        assert new_run == (True,)
        connection.rollback()


def test_migration_006_requires_active_then_completed_commit_for_final_output(
    migration_006_database: Migration006Database,
) -> None:
    with connect(migration_006_database.conninfo) as connection:
        no_commit_run_id = _create_review_required_run(
            connection,
            migration_006_database.pile_id,
        )
        with pytest.raises(RaiseException, match="active COMMIT RunStep"), connection.transaction():
            _insert_final_report(connection, no_commit_run_id, "c")

        running_commit_run_id = _create_review_required_run(
            connection,
            migration_006_database.pile_id,
        )
        _start_commit_step(connection, running_commit_run_id)
        _insert_final_report(connection, running_commit_run_id, "d")
        with pytest.raises(
            RaiseException,
            match="Run completion requires completed COMMIT RunStep",
        ), connection.transaction():
            connection.execute(
                """
                UPDATE runs
                SET status = 'COMPLETED'::run_status, completed_at = clock_timestamp()
                WHERE id = %s::uuid
                """,
                (running_commit_run_id,),
            )
        connection.rollback()


def test_migration_006_allows_completion_with_final_report_and_completed_commit(
    migration_006_database: Migration006Database,
) -> None:
    with connect(migration_006_database.conninfo) as connection:
        run_id = _create_review_required_run(connection, migration_006_database.pile_id)
        _start_commit_step(connection, run_id)
        _insert_final_report(connection, run_id, "e")
        connection.execute(
            """
            UPDATE run_steps
            SET status = 'COMPLETED'::run_step_status, completed_at = clock_timestamp()
            WHERE run_id = %s::uuid AND stage = 'COMMIT'::run_stage AND attempt = 1
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
            SELECT run_id, stage, attempt,
                   FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT
            FROM run_steps
            WHERE run_id = %s::uuid AND stage = 'COMMIT'::run_stage AND attempt = 1
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE runs
            SET status = 'COMPLETED'::run_status, completed_at = clock_timestamp()
            WHERE id = %s::uuid
            """,
            (run_id,),
        )
        connection.commit()

        proof = connection.execute(
            """
            SELECT r.status::text, s.status::text, count(o.id), count(m.id)
            FROM runs r
            JOIN run_steps s
              ON s.run_id = r.id AND s.stage = 'COMMIT'::run_stage AND s.attempt = 1
            LEFT JOIN outputs o
              ON o.run_id = r.id AND o.output_type = 'FINAL_REPORT'
            LEFT JOIN run_metrics m
              ON m.run_id = s.run_id AND m.stage = s.stage AND m.attempt = s.attempt
            WHERE r.id = %s::uuid
            GROUP BY r.status, s.status
            """,
            (run_id,),
        ).fetchone()
        assert proof == ("COMPLETED", "COMPLETED", 1, 1)


def test_migration_006_makes_ready_and_failed_document_versions_immutable(
    migration_006_database: Migration006Database,
) -> None:
    with connect(migration_006_database.conninfo) as connection:
        for version_id, terminal_status in (
            (migration_006_database.ready_version_id, "READY"),
            (migration_006_database.failed_version_id, "FAILED"),
        ):
            with pytest.raises(
                RaiseException,
                match=rf"{terminal_status} document version .* is immutable",
            ), connection.transaction():
                connection.execute(
                    """
                    UPDATE document_versions
                    SET storage_uri = storage_uri || '-mutated'
                    WHERE id = %s::uuid
                    """,
                    (version_id,),
                )
        connection.rollback()


def test_migration_006_rejects_cross_pile_tail_and_parent_mutation(
    migration_006_database: Migration006Database,
) -> None:
    with connect(migration_006_database.conninfo) as connection:
        pile_a = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"migration-006-pile-a-{uuid4()}",),
        ).fetchone()
        pile_b = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"migration-006-pile-b-{uuid4()}",),
        ).fetchone()
        assert pile_a is not None and pile_b is not None
        other_pile_run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s, 'MANUAL'::run_type, 'PENDING'::run_status)
            RETURNING id
            """,
            (pile_b[0],),
        ).fetchone()
        assert other_pile_run is not None

        with (
            pytest.raises(RaiseException, match="must reference a Run in the same pile"),
            connection.transaction(),
        ):
            connection.execute(
                "UPDATE piles SET latest_run_id = %s WHERE id = %s",
                (other_pile_run[0], pile_a[0]),
            )

        parent_one = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s, 'MANUAL'::run_type, 'PENDING'::run_status)
            RETURNING id
            """,
            (pile_a[0],),
        ).fetchone()
        parent_two = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s, 'MANUAL'::run_type, 'PENDING'::run_status)
            RETURNING id
            """,
            (pile_a[0],),
        ).fetchone()
        assert parent_one is not None and parent_two is not None
        child = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, parent_run_id)
            VALUES (%s, 'MANUAL'::run_type, 'PENDING'::run_status, %s)
            RETURNING id
            """,
            (pile_a[0], parent_one[0]),
        ).fetchone()
        assert child is not None
        with (
            pytest.raises(RaiseException, match="parent_run_id is immutable once set"),
            connection.transaction(),
        ):
            connection.execute(
                "UPDATE runs SET parent_run_id = %s WHERE id = %s",
                (parent_two[0], child[0]),
            )
        connection.rollback()


def _provider_usage_insert(
    connection,
    *,
    run_id: str,
    attempt: int,
    state: str,
    recorded_at: datetime | None,
    input_tokens: int | None,
    output_tokens: int | None,
    estimated_cost: Decimal | None,
) -> None:
    connection.execute(
        """
        INSERT INTO provider_usage_events (
            run_id, stage, attempt, operation_scope, operation_key, claim_generation,
            provider_name, model_name, state, input_tokens, output_tokens,
            estimated_cost, recorded_at
        ) VALUES (
            %s::uuid, 'UNDERSTAND'::run_stage, %s, 'migration-006-test', %s, 1,
            'fake', 'fake-v1', %s::provider_usage_state, %s, %s, %s, %s
        )
        """,
        (
            run_id,
            attempt,
            f"usage:{uuid4()}",
            state,
            input_tokens,
            output_tokens,
            estimated_cost,
            recorded_at,
        ),
    )


def test_migration_006_provider_usage_fk_and_exact_state_constraints(
    migration_006_database: Migration006Database,
) -> None:
    recorded_at = datetime.now(UTC)
    with connect(migration_006_database.conninfo) as connection:
        run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL'::run_type, 'RUNNING'::run_status)
            RETURNING id
            """,
            (migration_006_database.pile_id,),
        ).fetchone()
        assert run is not None
        run_id = str(run[0])
        connection.execute(
            """
            INSERT INTO run_steps (run_id, stage, status, attempt)
            VALUES (%s::uuid, 'UNDERSTAND'::run_stage, 'RUNNING'::run_step_status, 1)
            """,
            (run_id,),
        )

        with pytest.raises(ForeignKeyViolation), connection.transaction():
            _provider_usage_insert(
                connection,
                run_id=run_id,
                attempt=2,
                state="STARTED",
                recorded_at=None,
                input_tokens=None,
                output_tokens=None,
                estimated_cost=None,
            )

        invalid_states = (
            ("STARTED", recorded_at, None, None, None),
            ("STARTED", None, 1, None, None),
            ("UNAVAILABLE", None, None, None, None),
            ("UNAVAILABLE", recorded_at, 1, None, None),
            ("UNAVAILABLE", recorded_at, None, 1, None),
            ("UNAVAILABLE", recorded_at, None, None, Decimal("0.01")),
            ("RECORDED", None, None, None, None),
            ("RECORDED", recorded_at, -1, None, None),
        )
        for state, finished_at, input_tokens, output_tokens, estimated_cost in invalid_states:
            with pytest.raises(CheckViolation), connection.transaction():
                _provider_usage_insert(
                    connection,
                    run_id=run_id,
                    attempt=1,
                    state=state,
                    recorded_at=finished_at,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                )

        _provider_usage_insert(
            connection,
            run_id=run_id,
            attempt=1,
            state="STARTED",
            recorded_at=None,
            input_tokens=None,
            output_tokens=None,
            estimated_cost=None,
        )
        _provider_usage_insert(
            connection,
            run_id=run_id,
            attempt=1,
            state="UNAVAILABLE",
            recorded_at=recorded_at,
            input_tokens=None,
            output_tokens=None,
            estimated_cost=None,
        )
        _provider_usage_insert(
            connection,
            run_id=run_id,
            attempt=1,
            state="RECORDED",
            recorded_at=recorded_at,
            input_tokens=13,
            output_tokens=None,
            estimated_cost=None,
        )
        states = connection.execute(
            """
            SELECT state::text, input_tokens, output_tokens, estimated_cost
            FROM provider_usage_events
            WHERE run_id = %s::uuid
            ORDER BY state::text
            """,
            (run_id,),
        ).fetchall()
        assert states == [
            ("RECORDED", 13, None, None),
            ("STARTED", None, None, None),
            ("UNAVAILABLE", None, None, None),
        ]
        connection.rollback()
