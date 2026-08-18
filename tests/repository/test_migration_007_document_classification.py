from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import RaiseException

from config.settings import get_settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = tuple(
    _PROJECT_ROOT / "sql" / "migrations" / name
    for name in (
        "001_schema_v2_7.sql",
        "002_run_rule_scope.sql",
        "003_update_review.sql",
        "004_idempotency_claim_generation.sql",
        "005_run_step_retry_history.sql",
        "006_document_runtime_commit_and_telemetry.sql",
        "007_watch_folder_document_classification.sql",
    )
)


def test_migration_007_enforces_ready_grounded_immutable_classification() -> None:
    name = f"task1_phase6c1_migration_{uuid4().hex}"
    info = conninfo_to_dict(get_settings().database_url)
    info["dbname"] = "postgres"
    admin_conninfo = make_conninfo(**info)
    with connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    database_info = dict(info)
    database_info["dbname"] = name
    conninfo = make_conninfo(**database_info)
    try:
        with connect(conninfo) as connection:
            for migration in _MIGRATIONS:
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.commit()

            assert connection.execute(
                """
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                """,
            ).fetchone() == (29,)

            pile_id = connection.execute(
                "INSERT INTO piles (name) VALUES ('migration-007') RETURNING id",
            ).fetchone()[0]
            document_id = connection.execute(
                "INSERT INTO documents (pile_id, logical_key) VALUES (%s, 'doc') RETURNING id",
                (pile_id,),
            ).fetchone()[0]
            pending_version = connection.execute(
                """
                INSERT INTO document_versions (
                    document_id, version_number, content_hash, storage_uri
                ) VALUES (%s, 1, %s, %s) RETURNING id
                """,
                (document_id, "sha256:" + "1" * 64, "source://sha256/" + "1" * 64),
            ).fetchone()[0]
            with pytest.raises(RaiseException, match="READY DocumentVersion"), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO document_classifications (
                        document_version_id, role, confidence, explanation,
                        input_fingerprint, provider_name
                    ) VALUES (%s, 'UNKNOWN', 0.1, 'unknown', %s, 'test')
                    """,
                    (pending_version, "sha256:" + "a" * 64),
                )

            connection.execute(
                "UPDATE document_versions SET processing_status = 'READY' WHERE id = %s",
                (pending_version,),
            )
            chunk_id = connection.execute(
                """
                INSERT INTO document_chunks (
                    document_version_id, chunk_index, content, content_hash
                ) VALUES (%s, 0, 'Loan Estimate', %s) RETURNING id
                """,
                (pending_version, "sha256:" + "b" * 64),
            ).fetchone()[0]
            with pytest.raises(RaiseException, match="requires grounded"), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO document_classifications (
                        document_version_id, role, confidence, explanation,
                        input_fingerprint, provider_name
                    ) VALUES (%s, 'LOAN_ESTIMATE', 0.9, 'ungrounded', %s, 'test')
                    """,
                    (pending_version, "sha256:" + "e" * 64),
                )
                # The grounding rule is intentionally DEFERRABLE so the service can
                # insert the classification row and its evidence in one transaction.
                # Force deferred constraints here to prove an ungrounded known role
                # cannot commit, even though this assertion runs inside a savepoint.
                connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

            classification_id = connection.execute(
                """
                INSERT INTO document_classifications (
                    document_version_id, role, confidence, explanation,
                    input_fingerprint, provider_name
                ) VALUES (%s, 'LOAN_ESTIMATE', 0.99, 'grounded', %s, 'test')
                RETURNING id
                """,
                (pending_version, "sha256:" + "c" * 64),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO document_classification_evidence (
                    classification_id, chunk_id, evidence_order
                ) VALUES (%s, %s, 0)
                """,
                (classification_id, chunk_id),
            )
            connection.commit()

            second_document = connection.execute(
                "INSERT INTO documents (pile_id, logical_key) VALUES (%s, 'other') RETURNING id",
                (pile_id,),
            ).fetchone()[0]
            second_version = connection.execute(
                """
                INSERT INTO document_versions (
                    document_id, version_number, content_hash, storage_uri, processing_status
                ) VALUES (%s, 1, %s, %s, 'READY') RETURNING id
                """,
                (second_document, "sha256:" + "2" * 64, "source://sha256/" + "2" * 64),
            ).fetchone()[0]
            wrong_chunk = connection.execute(
                """
                INSERT INTO document_chunks (
                    document_version_id, chunk_index, content, content_hash
                ) VALUES (%s, 0, 'Other source', %s) RETURNING id
                """,
                (second_version, "sha256:" + "d" * 64),
            ).fetchone()[0]
            with pytest.raises(RaiseException, match="must belong"), connection.transaction():
                connection.execute(
                    """
                    INSERT INTO document_classification_evidence (
                        classification_id, chunk_id, evidence_order
                    ) VALUES (%s, %s, 1)
                    """,
                    (classification_id, wrong_chunk),
                )

            with pytest.raises(RaiseException, match="immutable"), connection.transaction():
                connection.execute(
                    "UPDATE document_classifications SET confidence = 0.5 WHERE id = %s",
                    (classification_id,),
                )
    finally:
        with connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
