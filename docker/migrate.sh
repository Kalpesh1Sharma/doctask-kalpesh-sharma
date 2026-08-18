#!/bin/sh
set -eu

: "${POSTGRES_HOST:=postgres}"
: "${POSTGRES_DB:=task1}"
: "${POSTGRES_USER:=task1}"
: "${POSTGRES_PASSWORD:=task1_dev_password}"

export PGPASSWORD="$POSTGRES_PASSWORD"

psql_base() {
    psql -v ON_ERROR_STOP=1 -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

psql_base -q <<'SQL'
CREATE SCHEMA IF NOT EXISTS task1_meta;
CREATE TABLE IF NOT EXISTS task1_meta.schema_migrations (
    filename TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    adopted_existing BOOLEAN NOT NULL DEFAULT FALSE,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
SQL

marker_sql() {
    case "$1" in
        001_schema_v2_7.sql)
            printf "%s" "SELECT to_regclass('public.run_metrics') IS NOT NULL"
            ;;
        002_run_rule_scope.sql)
            printf "%s" "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_validate_finding_origin_rule_snapshot' AND NOT tgisinternal)"
            ;;
        003_update_review.sql)
            printf "%s" "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_update_review_decision_immutable' AND NOT tgisinternal)"
            ;;
        004_idempotency_claim_generation.sql)
            printf "%s" "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_idempotency_in_progress_claimed_at')"
            ;;
        005_run_step_retry_history.sql)
            printf "%s" "SELECT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname='trg_require_closed_step_metric' AND NOT tgisinternal)"
            ;;
        006_document_runtime_commit_and_telemetry.sql)
            printf "%s" "SELECT to_regclass('public.provider_usage_events') IS NOT NULL"
            ;;
        007_watch_folder_document_classification.sql)
            printf "%s" "SELECT to_regclass('public.document_classifications') IS NOT NULL"
            ;;
        008_item_level_update_review.sql)
            printf "%s" "SELECT to_regclass('public.update_item_review_decisions') IS NOT NULL"
            ;;
        *)
            echo "No deployment marker is defined for migration $1" >&2
            exit 2
            ;;
    esac
}

for path in /migrations/*.sql; do
    filename=$(basename "$path")
    checksum=$(sha256sum "$path" | awk '{print $1}')
    recorded=$(psql_base -Atq -c "SELECT sha256 FROM task1_meta.schema_migrations WHERE filename = '$filename'")

    if [ -n "$recorded" ]; then
        if [ "$recorded" != "$checksum" ]; then
            echo "Migration checksum mismatch for $filename" >&2
            exit 3
        fi
        echo "migration $filename already recorded"
        continue
    fi

    marker=$(psql_base -Atq -c "$(marker_sql "$filename")")
    if [ "$marker" = "t" ]; then
        echo "adopting already-applied migration $filename"
        psql_base -q -c "INSERT INTO task1_meta.schema_migrations (filename, sha256, adopted_existing) VALUES ('$filename', '$checksum', TRUE)"
        continue
    fi

    echo "applying migration $filename"
    psql_base -f "$path"

    marker=$(psql_base -Atq -c "$(marker_sql "$filename")")
    if [ "$marker" != "t" ]; then
        echo "Migration $filename completed without its expected schema marker" >&2
        exit 4
    fi

    psql_base -q -c "INSERT INTO task1_meta.schema_migrations (filename, sha256, adopted_existing) VALUES ('$filename', '$checksum', FALSE)"
done

public_table_count=$(psql_base -Atq -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
recorded_count=$(psql_base -Atq -c "SELECT count(*) FROM task1_meta.schema_migrations")

echo "migrations ready: recorded=$recorded_count public_tables=$public_table_count"
