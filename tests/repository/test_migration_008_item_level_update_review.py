from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import RaiseException
from psycopg.types.json import Jsonb

from config.settings import get_settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS_001_TO_007 = tuple(
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
_MIGRATION_008 = _PROJECT_ROOT / "sql" / "migrations" / "008_item_level_update_review.sql"


def _fingerprint(character: str) -> str:
    return f"sha256:{character * 64}"


@contextmanager
def _isolated_database(label: str):
    name = f"task1_phase6c2b_{label}_{uuid4().hex}"
    info = conninfo_to_dict(get_settings().database_url)
    info["dbname"] = "postgres"
    admin_conninfo = make_conninfo(**info)
    with connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    database_info = dict(info)
    database_info["dbname"] = name
    try:
        yield make_conninfo(**database_info)
    finally:
        with connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _migrate(connection, migrations: tuple[Path, ...]) -> None:
    for migration in migrations:
        connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()


def _seed_itemized_change_set(connection) -> tuple[str, str, tuple[str, str]]:
    pile_id = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"migration-008-{uuid4()}",),
    ).fetchone()[0]
    document_id = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s, %s) RETURNING id",
        (pile_id, f"doc-{uuid4()}"),
    ).fetchone()[0]
    version_id = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        ) VALUES (%s, 1, %s, %s, 'READY') RETURNING id
        """,
        (document_id, _fingerprint("a"), f"source://sha256/{'a' * 64}"),
    ).fetchone()[0]
    baseline_run_id = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, completed_at)
        VALUES (%s, 'MANUAL', 'COMPLETED', clock_timestamp()) RETURNING id
        """,
        (pile_id,),
    ).fetchone()[0]
    update_run_id = connection.execute(
        """
        INSERT INTO runs (
            pile_id, run_type, status, trigger_document_version_id, baseline_run_id
        ) VALUES (%s, 'UPDATE', 'REVIEW_REQUIRED', %s, %s) RETURNING id
        """,
        (pile_id, version_id, baseline_run_id),
    ).fetchone()[0]
    created_fact_id = uuid4()
    removed_fact_id = uuid4()
    connection.execute(
        """
        INSERT INTO facts (id, origin_run_id, subject, predicate, object_value, status)
        VALUES
            (%s, %s, 'loan', 'amount', %s::jsonb, 'CONFIRMED'),
            (%s, %s, 'loan', 'rate', %s::jsonb, 'CONFIRMED')
        """,
        (
            created_fact_id,
            update_run_id,
            Jsonb({"value": 250000}),
            removed_fact_id,
            baseline_run_id,
            Jsonb({"value": 6.25}),
        ),
    )
    manifest = {
        "format": "update-change-set-v1",
        "run_id": str(update_run_id),
        "baseline_run_id": str(baseline_run_id),
        "trigger_document_version_id": str(version_id),
        "preparation_fingerprint": _fingerprint("b"),
        "document_delta": [],
        "fact_delta": [
            {"fact_id": str(created_fact_id), "kind": "CREATED"},
            {"fact_id": str(removed_fact_id), "kind": "REMOVED_FROM_CURRENT_STATE"},
        ],
        "review_state_fingerprint": _fingerprint("c"),
        "render_input_fingerprint": _fingerprint("d"),
    }
    change_set_id = connection.execute(
        """
        INSERT INTO update_change_sets (
            run_id, baseline_run_id, trigger_document_version_id,
            preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
            manifest, change_set_fingerprint
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (
            update_run_id,
            baseline_run_id,
            version_id,
            _fingerprint("b"),
            _fingerprint("c"),
            _fingerprint("d"),
            Jsonb(manifest),
            _fingerprint("e"),
        ),
    ).fetchone()[0]
    return str(update_run_id), str(change_set_id), (str(created_fact_id), str(removed_fact_id))


def test_migration_008_materializes_and_enforces_item_level_update_review() -> None:
    with _isolated_database("fresh") as conninfo, connect(conninfo) as connection:
        _migrate(connection, _MIGRATIONS_001_TO_007 + (_MIGRATION_008,))

        assert connection.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """,
        ).fetchone() == (31,)

        _run_id, change_set_id, fact_ids = _seed_itemized_change_set(connection)
        items = connection.execute(
            """
            SELECT fact_id::text, kind::text, payload
            FROM update_change_items
            WHERE change_set_id = %s
            ORDER BY fact_id
            """,
            (change_set_id,),
        ).fetchall()
        assert {row[0] for row in items} == set(fact_ids)
        assert {row[1] for row in items} == {"FACT_CREATED", "FACT_REMOVED"}
        assert {row[2]["fact_id"] for row in items} == set(fact_ids)

        with pytest.raises(RaiseException, match="requires item-level review"), connection.transaction():
            connection.execute(
                """
                INSERT INTO update_review_decisions (
                    change_set_id, decision, actor_type, actor_id, idempotency_key
                ) VALUES (%s, 'APPROVED', 'HUMAN', 'reviewer', %s)
                """,
                (change_set_id, f"legacy-whole:{uuid4()}"),
            )

        item_ids = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT fact_id::text, id::text
                FROM update_change_items
                WHERE change_set_id = %s
                """,
                (change_set_id,),
            ).fetchall()
        }
        connection.execute(
            """
            INSERT INTO update_item_review_decisions (
                item_id, decision, actor_type, actor_id, idempotency_key
            ) VALUES (%s, 'APPROVED', 'HUMAN', 'reviewer', %s)
            """,
            (item_ids[fact_ids[0]], f"item:{uuid4()}"),
        )
        connection.execute(
            """
            INSERT INTO update_item_review_decisions (
                item_id, decision, actor_type, actor_id, idempotency_key
            ) VALUES (%s, 'REJECTED', 'HUMAN', 'reviewer', %s)
            """,
            (item_ids[fact_ids[1]], f"item:{uuid4()}"),
        )
        connection.commit()
        assert connection.execute(
            """
            SELECT decision::text FROM update_item_review_decisions AS decisions
            JOIN update_change_items AS items ON items.id = decisions.item_id
            WHERE items.change_set_id = %s ORDER BY items.fact_id
            """,
            (change_set_id,),
        ).fetchall() in ([('APPROVED',), ('REJECTED',)], [('REJECTED',), ('APPROVED',)])

        with pytest.raises(RaiseException, match="immutable"), connection.transaction():
            connection.execute(
                "UPDATE update_item_review_decisions SET actor_id = 'other' WHERE item_id = %s",
                (item_ids[fact_ids[0]],),
            )


def test_migration_008_backfills_existing_whole_set_decision_per_material_item() -> None:
    with _isolated_database("backfill") as conninfo, connect(conninfo) as connection:
        _migrate(connection, _MIGRATIONS_001_TO_007)
        _run_id, change_set_id, fact_ids = _seed_itemized_change_set(connection)
        connection.execute(
            """
            INSERT INTO update_review_decisions (
                change_set_id, decision, actor_type, actor_id, idempotency_key
            ) VALUES (%s, 'APPROVED', 'HUMAN', 'legacy-reviewer', 'legacy-key')
            """,
            (change_set_id,),
        )
        connection.commit()

        connection.execute(_MIGRATION_008.read_text(encoding="utf-8"))
        connection.commit()

        rows = connection.execute(
            """
            SELECT items.fact_id::text, decisions.decision::text, decisions.actor_id,
                   decisions.idempotency_key
            FROM update_change_items AS items
            JOIN update_item_review_decisions AS decisions ON decisions.item_id = items.id
            WHERE items.change_set_id = %s
            ORDER BY items.fact_id
            """,
            (change_set_id,),
        ).fetchall()
        assert {row[0] for row in rows} == set(fact_ids)
        assert {row[1] for row in rows} == {"APPROVED"}
        assert {row[2] for row in rows} == {"legacy-reviewer"}
        assert all(row[3].startswith("legacy-key:item:") for row in rows)
