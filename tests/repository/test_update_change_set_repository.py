from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException, UniqueViolation
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from api.application.finalize_run_service import (
    FinalizeRunOutputCommand,
    FinalizeRunService,
)
from config.settings import get_settings
from db.pool import pool
from domain.update_preparation import UpdateDocumentChange, UpdateDocumentChangeKind
from domain.update_review import (
    UpdateChangeSetIntegrityError,
    UpdateChangeSetManifest,
)
from repositories.update_change_set_repository import UpdateChangeSetRepository
from services.local_artifact_storage import LocalArtifactStorage


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def fingerprint(character: str) -> str:
    return f"sha256:{character * 64}"


def reviewable_update(connection) -> tuple[str, str, str]:
    pile = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"change-set-repository-{uuid4()}",),
    ).fetchone()
    assert pile is not None
    document = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
        (str(pile[0]), f"key-{uuid4()}"),
    ).fetchone()
    assert document is not None
    version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        ) VALUES (%s::uuid, 1, %s, %s, 'READY') RETURNING id
        """,
        (str(document[0]), fingerprint("a"), f"storage://{uuid4()}"),
    ).fetchone()
    baseline = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, completed_at)
        VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now()) RETURNING id
        """,
        (str(pile[0]),),
    ).fetchone()
    assert version is not None and baseline is not None
    update = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
        VALUES (%s::uuid, 'UPDATE', 'REVIEW_REQUIRED', %s::uuid, %s::uuid)
        RETURNING id
        """,
        (str(pile[0]), str(version[0]), str(baseline[0])),
    ).fetchone()
    assert update is not None
    return str(update[0]), str(baseline[0]), str(version[0])


def manifest(run_id: str, baseline_run_id: str, trigger_version_id: str) -> UpdateChangeSetManifest:
    return UpdateChangeSetManifest(
        run_id=run_id,
        baseline_run_id=baseline_run_id,
        trigger_document_version_id=trigger_version_id,
        preparation_fingerprint=fingerprint("a"),
        document_delta=(
            UpdateDocumentChange(
                document_id=str(uuid4()),
                kind=UpdateDocumentChangeKind.ADDED,
                baseline_document_version_id=None,
                target_document_version_id=trigger_version_id,
                target_version_number=1,
                target_content_hash=fingerprint("b"),
            ),
        ),
        fact_delta=(),
        review_state_fingerprint=fingerprint("c"),
        render_input_fingerprint=fingerprint("d"),
    )


def test_create_get_and_self_integrity_validation() -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        value = manifest(run_id, baseline_run_id, trigger_version_id)
        repository = UpdateChangeSetRepository(connection)
        created = repository.create(value)
        loaded = repository.get_by_run_id(run_id)
        assert loaded is not None
        assert loaded.change_set_id == created.change_set_id
        assert loaded.to_manifest() == value
        connection.rollback()


def test_change_set_is_unique_per_update_run() -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        repository = UpdateChangeSetRepository(connection)
        repository.create(row_manifest)
        with pytest.raises(UniqueViolation), connection.transaction():
            repository.create(row_manifest)
        connection.rollback()


@pytest.mark.parametrize(
    "key",
    [
        "run_id",
        "baseline_run_id",
        "trigger_document_version_id",
        "preparation_fingerprint",
        "review_state_fingerprint",
        "render_input_fingerprint",
    ],
)
def test_database_rejects_manifest_scalar_split_brain(key: str) -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        value = manifest(run_id, baseline_run_id, trigger_version_id)
        payload = value.model_dump(mode="json")
        payload[key] = str(uuid4()) if key.endswith("id") else fingerprint("e")
        with pytest.raises(RaiseException):
            connection.execute(
                """
                INSERT INTO update_change_sets (
                    run_id, baseline_run_id, trigger_document_version_id,
                    preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                    manifest, change_set_fingerprint
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    value.run_id,
                    value.baseline_run_id,
                    value.trigger_document_version_id,
                    value.preparation_fingerprint,
                    value.review_state_fingerprint,
                    value.render_input_fingerprint,
                    Jsonb(payload),
                    fingerprint("f"),
                ),
            )
        connection.rollback()


def test_shape_valid_but_wrong_stored_fingerprint_is_application_integrity_error() -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        value = manifest(run_id, baseline_run_id, trigger_version_id)
        connection.execute(
            """
            INSERT INTO update_change_sets (
                run_id, baseline_run_id, trigger_document_version_id,
                preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                manifest, change_set_fingerprint
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                value.run_id,
                value.baseline_run_id,
                value.trigger_document_version_id,
                value.preparation_fingerprint,
                value.review_state_fingerprint,
                value.render_input_fingerprint,
                Jsonb(value.model_dump(mode="json")),
                fingerprint("0"),
            ),
        )
        loaded = UpdateChangeSetRepository(connection).get_by_run_id(run_id)
        assert loaded is not None
        with pytest.raises(UpdateChangeSetIntegrityError):
            loaded.to_manifest()
        connection.rollback()


def test_migrations_apply_to_a_clean_database_in_order(tmp_path: Path) -> None:
    """Exercise 001 -> 002 -> 003 -> 004 -> 005 -> 006 without shared state."""
    database_name = f"task1_phase4b_{uuid4().hex}"
    connection_info = conninfo_to_dict(get_settings().database_url)
    connection_info["dbname"] = "postgres"
    admin_info = make_conninfo(**connection_info)
    migration_paths = (
        Path("sql/migrations/001_schema_v2_7.sql"),
        Path("sql/migrations/002_run_rule_scope.sql"),
        Path("sql/migrations/003_update_review.sql"),
        Path("sql/migrations/004_idempotency_claim_generation.sql"),
        Path("sql/migrations/005_run_step_retry_history.sql"),
        Path("sql/migrations/006_document_runtime_commit_and_telemetry.sql"),
    )
    historical_fingerprint = fingerprint("f")
    historical_storage = LocalArtifactStorage(tmp_path / database_name)
    historical_artifact = historical_storage.publish_bytes(b"historical final report")
    historical_completed_run_id: str | None = None
    historical_active_run_id: str | None = None
    with connect(admin_info, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        database_info = dict(connection_info)
        database_info["dbname"] = database_name
        with connect(make_conninfo(**database_info)) as isolated:
            for migration_path in migration_paths:
                if migration_path.name == "004_idempotency_claim_generation.sql":
                    isolated.execute(
                        "INSERT INTO idempotency_records (scope, key, status, claimed_at) VALUES ('migration-004', 'legacy', 'COMPLETED'::idempotency_status, clock_timestamp())",
                    )
                if migration_path.name == "005_run_step_retry_history.sql":
                    pile = isolated.execute(
                        "INSERT INTO piles (name) VALUES ('migration-005-history') RETURNING id",
                    ).fetchone()
                    assert pile is not None
                    run = isolated.execute(
                        """INSERT INTO runs (pile_id, run_type, status)
                           VALUES (%s, 'MANUAL'::run_type, 'PENDING'::run_status) RETURNING id""",
                        (pile[0],),
                    ).fetchone()
                    assert run is not None
                    isolated.execute(
                        """INSERT INTO run_steps (run_id, stage, status, attempt, started_at, completed_at)
                           VALUES (%s, 'INGEST'::run_stage, 'COMPLETED'::run_step_status, 1,
                                   clock_timestamp() - interval '2 seconds', clock_timestamp())""",
                        (run[0],),
                    )
                    isolated.execute(
                        """INSERT INTO run_metrics (run_id, stage, attempt, duration_ms, input_tokens, estimated_cost)
                           VALUES (%s, 'INGEST'::run_stage, 1, NULL, 7, 1.25)""",
                        (run[0],),
                    )
                    isolated.execute(
                        """INSERT INTO idempotency_records (scope, key, status, claimed_at)
                           VALUES ('migration-005', 'historical-failure', 'FAILED'::idempotency_status, clock_timestamp())""",
                    )
                if migration_path.name == "006_document_runtime_commit_and_telemetry.sql":
                    cutover_pile = isolated.execute(
                        "INSERT INTO piles (name) VALUES ('migration-006-cutover') RETURNING id",
                    ).fetchone()
                    assert cutover_pile is not None
                    historical_completed = isolated.execute(
                        """
                        INSERT INTO runs (pile_id, run_type, status, completed_at)
                        VALUES (%s, 'MANUAL'::run_type, 'COMPLETED'::run_status, clock_timestamp())
                        RETURNING id
                        """,
                        (cutover_pile[0],),
                    ).fetchone()
                    historical_active = isolated.execute(
                        """
                        INSERT INTO runs (pile_id, run_type, status)
                        VALUES (%s, 'MANUAL'::run_type, 'REVIEW_REQUIRED'::run_status)
                        RETURNING id
                        """,
                        (cutover_pile[0],),
                    ).fetchone()
                    assert historical_completed is not None and historical_active is not None
                    historical_completed_run_id = str(historical_completed[0])
                    historical_active_run_id = str(historical_active[0])
                    historical_output = isolated.execute(
                        """
                        INSERT INTO outputs (run_id, output_type, storage_uri, content_hash)
                        VALUES (%s, 'FINAL_REPORT', %s, %s)
                        RETURNING id
                        """,
                        (
                            historical_completed[0],
                            historical_artifact.storage_uri,
                            historical_artifact.content_hash,
                        ),
                    ).fetchone()
                    assert historical_output is not None
                    isolated.execute(
                        """
                        INSERT INTO audit_events (
                            run_id, entity_type, entity_id, event_type, actor_type, actor_id, payload
                        )
                        VALUES (%s, 'output', %s, 'FINAL_OUTPUT_COMMITTED', 'system', 'pre-006', %s::jsonb)
                        """,
                        (
                            historical_completed[0],
                            historical_output[0],
                            Jsonb(
                                {
                                    "run_id": historical_completed_run_id,
                                    "output_type": "FINAL_REPORT",
                                    "render_input_fingerprint": historical_fingerprint,
                                    "artifact_content_hash": historical_artifact.content_hash,
                                    "storage_uri": historical_artifact.storage_uri,
                                },
                            ),
                        ),
                    )
                    for stage in ("INGEST", "UNDERSTAND", "EXAMINE", "REVIEW"):
                        isolated.execute(
                            """
                            INSERT INTO run_steps (run_id, stage, status, attempt, started_at, completed_at)
                            VALUES (%s, %s::run_stage, 'COMPLETED'::run_step_status, 1,
                                    clock_timestamp() - interval '1 second', clock_timestamp())
                            """,
                            (historical_active[0], stage),
                        )
                        isolated.execute(
                            """
                            INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
                            SELECT run_id, stage, attempt,
                                   FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT
                            FROM run_steps
                            WHERE run_id = %s
                              AND stage = %s::run_stage
                              AND attempt = 1
                            """,
                            (historical_active[0], stage),
                        )
                isolated.execute(migration_path.read_text(encoding="utf-8"))
                isolated.commit()
            table_count = isolated.execute(
                """
                SELECT count(*)
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                """,
            ).fetchone()
            assert table_count == (27,)
            objects = isolated.execute(
                """
                SELECT to_regclass('public.update_change_sets'),
                       to_regclass('public.update_review_decisions'),
                       to_regprocedure('validate_update_change_set_insert()'),
                       to_regprocedure('validate_update_review_decision_insert()')
                """,
            ).fetchone()
            assert objects == (
                "update_change_sets",
                "update_review_decisions",
                "validate_update_change_set_insert()",
                "validate_update_review_decision_insert()",
            )
            generation = isolated.execute(
                "SELECT claim_generation FROM idempotency_records WHERE scope = 'migration-004' AND key = 'legacy'",
            ).fetchone()
            assert generation == (0,)
            default = isolated.execute(
                "SELECT column_default FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'idempotency_records' AND column_name = 'claim_generation'",
            ).fetchone()
            assert default is not None and "1" in str(default[0])
            future = isolated.execute(
                "INSERT INTO idempotency_records (scope, key, status, claimed_at) VALUES ('migration-004', 'future', 'IN_PROGRESS'::idempotency_status, clock_timestamp()) RETURNING claim_generation",
            ).fetchone()
            assert future == (1,)
            historical_failure = isolated.execute(
                "SELECT failure_kind FROM idempotency_records WHERE scope = 'migration-005'",
            ).fetchone()
            assert historical_failure == (None,)
            metric = isolated.execute(
                "SELECT duration_ms, input_tokens, estimated_cost FROM run_metrics WHERE attempt = 1",
            ).fetchone()
            assert metric is not None and metric[0] >= 0 and metric[1:] == (7, Decimal("1.25"))
            assert isolated.execute(
                "SELECT to_regclass('public.provider_usage_events')",
            ).fetchone() == ("provider_usage_events",)
            assert historical_completed_run_id is not None
            assert historical_active_run_id is not None
            cutover = isolated.execute(
                """
                SELECT id::text, commit_required
                FROM runs
                WHERE id IN (%s::uuid, %s::uuid)
                ORDER BY id
                """,
                (historical_completed_run_id, historical_active_run_id),
            ).fetchall()
            cutover_map = {str(row[0]): row[1] for row in cutover}
            assert cutover_map[historical_completed_run_id] is False
            assert cutover_map[historical_active_run_id] is True
            assert isolated.execute(
                "SELECT count(*) FROM run_steps WHERE run_id IN (%s::uuid, %s::uuid) AND stage = 'COMMIT'::run_stage",
                (historical_completed_run_id, historical_active_run_id),
            ).fetchone() == (0,)

            new_run = isolated.execute(
                """
                INSERT INTO runs (pile_id, run_type, status)
                SELECT pile_id, 'MANUAL'::run_type, 'REVIEW_REQUIRED'::run_status
                FROM runs WHERE id = %s::uuid
                RETURNING id, commit_required
                """,
                (historical_active_run_id,),
            ).fetchone()
            assert new_run is not None and new_run[1] is True
            isolated.commit()

            with pytest.raises(RaiseException, match="active COMMIT RunStep"):
                isolated.execute(
                    """
                    INSERT INTO outputs (run_id, output_type, storage_uri, content_hash)
                    VALUES (%s, 'FINAL_REPORT', 'artifact://bypass', %s)
                    """,
                    (new_run[0], fingerprint("b")),
                )
            isolated.rollback()

            with pytest.raises(RaiseException, match="Run completion requires FINAL_REPORT output"):
                isolated.execute(
                    "UPDATE runs SET status = 'COMPLETED'::run_status, completed_at = clock_timestamp() WHERE id = %s",
                    (new_run[0],),
                )
            isolated.rollback()

            with pytest.raises(RaiseException, match="new Run must require COMMIT completion proof"):
                isolated.execute(
                    """
                    INSERT INTO runs (pile_id, run_type, status, commit_required)
                    SELECT pile_id, 'MANUAL'::run_type, 'PENDING'::run_status, FALSE
                    FROM runs WHERE id = %s::uuid
                    """,
                    (historical_active_run_id,),
                )
            isolated.rollback()

            with pytest.raises(RaiseException, match="cutover marker is immutable"):
                isolated.execute(
                    "UPDATE runs SET commit_required = FALSE WHERE id = %s",
                    (new_run[0],),
                )
            isolated.rollback()

            isolated.commit()
            isolated_pool = ConnectionPool(
                conninfo=make_conninfo(**database_info),
                min_size=1,
                max_size=1,
                open=False,
            )
            isolated_pool.open()
            try:
                replay_service = FinalizeRunService(isolated_pool, historical_storage)
                replay = replay_service.finalize(
                    FinalizeRunOutputCommand(
                        run_id=historical_completed_run_id,
                        expected_render_input_fingerprint=historical_fingerprint,
                        artifact_storage_uri=historical_artifact.storage_uri,
                        artifact_content_hash=historical_artifact.content_hash,
                        actor_type="system",
                        actor_id="pre-006",
                    ),
                )
                assert replay.replayed is True
                assert replay.run.commit_required is False
            finally:
                isolated_pool.close()
    finally:
        with connect(admin_info, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


def test_migration_004_rejects_malformed_legacy_in_progress_claim() -> None:
    database_name = f"task1_phase5b_bad_{uuid4().hex}"
    connection_info = conninfo_to_dict(get_settings().database_url)
    connection_info["dbname"] = "postgres"
    admin_info = make_conninfo(**connection_info)
    migrations = (
        Path("sql/migrations/001_schema_v2_7.sql"),
        Path("sql/migrations/002_run_rule_scope.sql"),
        Path("sql/migrations/003_update_review.sql"),
    )
    with connect(admin_info, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        database_info = dict(connection_info)
        database_info["dbname"] = database_name
        with connect(make_conninfo(**database_info)) as isolated:
            for migration in migrations:
                isolated.execute(migration.read_text(encoding="utf-8"))
                isolated.commit()
            isolated.execute(
                "INSERT INTO idempotency_records (scope, key, status, claimed_at) VALUES ('bad-004', 'null-lease', 'IN_PROGRESS'::idempotency_status, NULL)",
            )
            isolated.commit()
            with pytest.raises(RaiseException, match="NULL claimed_at"):
                isolated.execute(
                    Path("sql/migrations/004_idempotency_claim_generation.sql").read_text(
                        encoding="utf-8",
                    ),
                )
            isolated.rollback()
            row = isolated.execute(
                "SELECT claimed_at FROM idempotency_records WHERE scope = 'bad-004'",
            ).fetchone()
            assert row == (None,)
    finally:
        with connect(admin_info, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("conflicting_duration", "historical duration differs"),
        ("orphan_metric", "metric has no closed RunStep"),
        ("open_metric", "metric has no closed RunStep"),
        (
            "missing_closed_timestamps",
            "closed RunStep has invalid timestamps|chk_closed_run_step_timestamps",
        ),
    ],
)
def test_migration_005_rejects_invalid_historical_retry_data(case: str, message: str) -> None:
    """Migration 005 fails closed rather than guessing historical retry truth."""
    database_name = f"task1_phase5c_bad_{case}_{uuid4().hex}"
    connection_info = conninfo_to_dict(get_settings().database_url)
    connection_info["dbname"] = "postgres"
    admin_info = make_conninfo(**connection_info)
    migrations = (
        Path("sql/migrations/001_schema_v2_7.sql"),
        Path("sql/migrations/002_run_rule_scope.sql"),
        Path("sql/migrations/003_update_review.sql"),
        Path("sql/migrations/004_idempotency_claim_generation.sql"),
    )
    with connect(admin_info, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        database_info = dict(connection_info)
        database_info["dbname"] = database_name
        with connect(make_conninfo(**database_info)) as isolated:
            for migration in migrations:
                isolated.execute(migration.read_text(encoding="utf-8"))
                isolated.commit()
            pile = isolated.execute(
                "INSERT INTO piles (name) VALUES (%s) RETURNING id",
                (f"bad-005-{case}",),
            ).fetchone()
            assert pile is not None
            run = isolated.execute(
                """INSERT INTO runs (pile_id, run_type, status)
                   VALUES (%s, 'MANUAL', 'PENDING') RETURNING id""",
                (pile[0],),
            ).fetchone()
            assert run is not None
            if case == "orphan_metric":
                isolated.execute(
                    """INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
                       VALUES (%s, 'INGEST', 1, 1)""",
                    (run[0],),
                )
            elif case == "open_metric":
                isolated.execute(
                    """INSERT INTO run_steps (run_id, stage, status, attempt)
                       VALUES (%s, 'INGEST', 'RUNNING', 1)""",
                    (run[0],),
                )
                isolated.execute(
                    """INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
                       VALUES (%s, 'INGEST', 1, 1)""",
                    (run[0],),
                )
            elif case == "missing_closed_timestamps":
                isolated.execute(
                    """INSERT INTO run_steps (run_id, stage, status, attempt)
                       VALUES (%s, 'INGEST', 'COMPLETED', 1)""",
                    (run[0],),
                )
            else:
                isolated.execute(
                    """INSERT INTO run_steps (run_id, stage, status, attempt, started_at, completed_at)
                       VALUES (%s, 'INGEST', 'COMPLETED', 1,
                               clock_timestamp() - interval '2 seconds', clock_timestamp())""",
                    (run[0],),
                )
                isolated.execute(
                    """INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
                       VALUES (%s, 'INGEST', 1, 1)""",
                    (run[0],),
                )
            isolated.commit()
            with pytest.raises((RaiseException, CheckViolation), match=message):
                isolated.execute(
                    Path("sql/migrations/005_run_step_retry_history.sql").read_text(
                        encoding="utf-8",
                    ),
                )
            isolated.rollback()
    finally:
        with connect(admin_info, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("format", "wrong-format"),
        ("run_id", None),
        ("baseline_run_id", []),
        ("trigger_document_version_id", 7),
        ("preparation_fingerprint", None),
        ("review_state_fingerprint", []),
        ("render_input_fingerprint", 7),
    ],
)
def test_database_rejects_missing_null_or_wrong_type_manifest_identity(
    key: str,
    value: object,
) -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        payload = row_manifest.model_dump(mode="json")
        payload[key] = value
        with pytest.raises(RaiseException):
            connection.execute(
                """
                INSERT INTO update_change_sets (
                    run_id, baseline_run_id, trigger_document_version_id,
                    preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                    manifest, change_set_fingerprint
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    row_manifest.run_id,
                    row_manifest.baseline_run_id,
                    row_manifest.trigger_document_version_id,
                    row_manifest.preparation_fingerprint,
                    row_manifest.review_state_fingerprint,
                    row_manifest.render_input_fingerprint,
                    Jsonb(payload),
                    fingerprint("f"),
                ),
            )
        connection.rollback()


@pytest.mark.parametrize(
    "key",
    (
        "format",
        "run_id",
        "baseline_run_id",
        "trigger_document_version_id",
        "preparation_fingerprint",
        "review_state_fingerprint",
        "render_input_fingerprint",
    ),
)
def test_database_rejects_missing_required_manifest_identity_key(key: str) -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        payload = row_manifest.model_dump(mode="json")
        payload.pop(key)
        with pytest.raises(RaiseException):
            connection.execute(
                """
                INSERT INTO update_change_sets (
                    run_id, baseline_run_id, trigger_document_version_id,
                    preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                    manifest, change_set_fingerprint
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    row_manifest.run_id,
                    row_manifest.baseline_run_id,
                    row_manifest.trigger_document_version_id,
                    row_manifest.preparation_fingerprint,
                    row_manifest.review_state_fingerprint,
                    row_manifest.render_input_fingerprint,
                    Jsonb(payload),
                    fingerprint("f"),
                ),
            )
        connection.rollback()


@pytest.mark.parametrize("column", ["preparation_fingerprint", "review_state_fingerprint", "render_input_fingerprint", "change_set_fingerprint"])
def test_database_rejects_noncanonical_fingerprint_shapes(column: str) -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        values = {
            "preparation_fingerprint": row_manifest.preparation_fingerprint,
            "review_state_fingerprint": row_manifest.review_state_fingerprint,
            "render_input_fingerprint": row_manifest.render_input_fingerprint,
            "change_set_fingerprint": fingerprint("f"),
        }
        values[column] = "sha256:" + "A" * 64
        payload = row_manifest.model_dump(mode="json")
        if column != "change_set_fingerprint":
            payload[column] = values[column]
        with pytest.raises(CheckViolation):
            connection.execute(
                """
                INSERT INTO update_change_sets (
                    run_id, baseline_run_id, trigger_document_version_id,
                    preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                    manifest, change_set_fingerprint
                ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    row_manifest.run_id,
                    row_manifest.baseline_run_id,
                    row_manifest.trigger_document_version_id,
                    values["preparation_fingerprint"],
                    values["review_state_fingerprint"],
                    values["render_input_fingerprint"],
                    Jsonb(payload),
                    values["change_set_fingerprint"],
                ),
            )
        connection.rollback()


def test_change_set_insert_requires_matching_reviewable_update_run() -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        connection.execute("UPDATE runs SET status = 'PENDING' WHERE id = %s::uuid", (run_id,))
        with pytest.raises(RaiseException):
            UpdateChangeSetRepository(connection).create(row_manifest)
        connection.rollback()

    with pool.connection() as connection:
        pile = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"manual-set-{uuid4()}",)).fetchone()
        assert pile is not None
        document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (str(pile[0]), f"manual-document-{uuid4()}"),
        ).fetchone()
        version = connection.execute(
            """INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri, processing_status)
               VALUES (%s::uuid, 1, %s, %s, 'READY') RETURNING id""",
            (str(document[0]), fingerprint("a"), f"storage://{uuid4()}"),
        ).fetchone()
        baseline = connection.execute(
            """INSERT INTO runs (pile_id, run_type, status, completed_at)
               VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now()) RETURNING id""",
            (str(pile[0]),),
        ).fetchone()
        manual = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'REVIEW_REQUIRED') RETURNING id",
            (str(pile[0]),),
        ).fetchone()
        assert version is not None and baseline is not None and manual is not None
        with pytest.raises(RaiseException):
            UpdateChangeSetRepository(connection).create(
                manifest(str(manual[0]), str(baseline[0]), str(version[0])),
            )
        connection.rollback()


def test_change_set_scalar_run_identity_and_immutable_delete_backstops() -> None:
    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        other_baseline = connection.execute(
            """INSERT INTO runs (pile_id, run_type, status, completed_at)
               SELECT pile_id, 'MANUAL', 'COMPLETED', now() FROM runs WHERE id = %s::uuid RETURNING id""",
            (run_id,),
        ).fetchone()
        assert other_baseline is not None
        payload = row_manifest.model_dump(mode="json")
        payload["baseline_run_id"] = str(other_baseline[0])
        with pytest.raises(RaiseException):
            connection.execute(
                """INSERT INTO update_change_sets (
                      run_id, baseline_run_id, trigger_document_version_id,
                      preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                      manifest, change_set_fingerprint)
                   VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)""",
                (
                    run_id,
                    str(other_baseline[0]),
                    trigger_version_id,
                    row_manifest.preparation_fingerprint,
                    row_manifest.review_state_fingerprint,
                    row_manifest.render_input_fingerprint,
                    Jsonb(payload),
                    fingerprint("f"),
                ),
            )
        connection.rollback()

    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        row_manifest = manifest(run_id, baseline_run_id, trigger_version_id)
        other_document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) SELECT pile_id, %s FROM runs WHERE id = %s::uuid RETURNING id",
            (f"other-trigger-{uuid4()}", run_id),
        ).fetchone()
        assert other_document is not None
        other_trigger = connection.execute(
            """INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri, processing_status)
               VALUES (%s::uuid, 1, %s, %s, 'READY') RETURNING id""",
            (str(other_document[0]), fingerprint("b"), f"storage://{uuid4()}"),
        ).fetchone()
        assert other_trigger is not None
        payload = row_manifest.model_dump(mode="json")
        payload["trigger_document_version_id"] = str(other_trigger[0])
        with pytest.raises(RaiseException):
            connection.execute(
                """INSERT INTO update_change_sets (
                      run_id, baseline_run_id, trigger_document_version_id,
                      preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                      manifest, change_set_fingerprint)
                   VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)""",
                (
                    run_id,
                    baseline_run_id,
                    str(other_trigger[0]),
                    row_manifest.preparation_fingerprint,
                    row_manifest.review_state_fingerprint,
                    row_manifest.render_input_fingerprint,
                    Jsonb(payload),
                    fingerprint("f"),
                ),
            )
        connection.rollback()

    with pool.connection() as connection:
        run_id, baseline_run_id, trigger_version_id = reviewable_update(connection)
        created = UpdateChangeSetRepository(connection).create(manifest(run_id, baseline_run_id, trigger_version_id))
        for statement, parameter in (
            ("UPDATE update_change_sets SET change_set_fingerprint = %s WHERE id = %s::uuid", (fingerprint("e"), created.change_set_id)),
            ("DELETE FROM update_change_sets WHERE id = %s::uuid", (created.change_set_id,)),
            ("DELETE FROM runs WHERE id = %s::uuid", (run_id,)),
            ("DELETE FROM runs WHERE id = %s::uuid", (baseline_run_id,)),
            ("DELETE FROM document_versions WHERE id = %s::uuid", (trigger_version_id,)),
        ):
            with pytest.raises((RaiseException, ForeignKeyViolation)), connection.transaction():
                connection.execute(statement, parameter)
        connection.rollback()
