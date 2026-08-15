from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException, UniqueViolation
from psycopg.types.json import Jsonb

from config.settings import get_settings
from db.pool import pool
from domain.update_preparation import UpdateDocumentChange, UpdateDocumentChangeKind
from domain.update_review import (
    UpdateChangeSetIntegrityError,
    UpdateChangeSetManifest,
)
from repositories.update_change_set_repository import UpdateChangeSetRepository


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


def test_migrations_apply_to_a_clean_database_in_order() -> None:
    """Exercise 001 -> 002 -> 003 without relying on the shared test database."""
    database_name = f"task1_phase4b_{uuid4().hex}"
    connection_info = conninfo_to_dict(get_settings().database_url)
    connection_info["dbname"] = "postgres"
    admin_info = make_conninfo(**connection_info)
    migration_paths = (
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
            for migration_path in migration_paths:
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
            assert table_count == (26,)
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
