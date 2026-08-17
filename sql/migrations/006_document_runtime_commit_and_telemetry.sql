-- Phase 6A: durable document runtime, COMMIT authority, and provider usage receipts.
-- This migration is additive.  It deliberately does not invent historical COMMIT
-- steps, lineage tails, provider receipts, or metric values.

BEGIN;

ALTER TYPE idempotency_failure_kind
    ADD VALUE IF NOT EXISTS 'RETRYABLE_ARTIFACT_PUBLICATION';

CREATE TYPE provider_usage_state AS ENUM ('STARTED', 'RECORDED', 'UNAVAILABLE');

-- Durable Phase-6 cutover marker. Historical Runs that were already COMPLETED
-- before this migration remain replayable without fabricated COMMIT history.
-- Every historical nonterminal Run and every Run created after this migration
-- must prove completion through the COMMIT stage.
ALTER TABLE runs
    ADD COLUMN commit_required BOOLEAN;

UPDATE runs
SET commit_required = (status <> 'COMPLETED'::run_status);

ALTER TABLE runs
    ALTER COLUMN commit_required SET DEFAULT TRUE,
    ALTER COLUMN commit_required SET NOT NULL;

-- The migration is the only authority allowed to assign FALSE.  Callers may
-- neither create a new compatibility Run nor downgrade a Phase-6 Run later.
CREATE OR REPLACE FUNCTION enforce_commit_required_cutover()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.commit_required IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'new Run must require COMMIT completion proof';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.commit_required IS DISTINCT FROM OLD.commit_required THEN
        RAISE EXCEPTION 'run commit_required cutover marker is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_enforce_commit_required_on_insert
BEFORE INSERT ON runs
FOR EACH ROW EXECUTE FUNCTION enforce_commit_required_cutover();

CREATE TRIGGER trg_enforce_commit_required_immutability
BEFORE UPDATE OF commit_required ON runs
FOR EACH ROW EXECUTE FUNCTION enforce_commit_required_cutover();

-- A receipt is tied to the semantic RunStep attempt which actually made the
-- provider call.  A Phase 5B generation takeover is a separate invocation and
-- therefore has a separate receipt; exact replays create no receipt.
CREATE TABLE provider_usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    stage run_stage NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    operation_scope TEXT NOT NULL CHECK (operation_scope <> ''),
    operation_key TEXT NOT NULL CHECK (operation_key <> ''),
    claim_generation BIGINT NOT NULL CHECK (claim_generation >= 0),
    provider_name TEXT NOT NULL CHECK (provider_name <> ''),
    model_name TEXT,
    state provider_usage_state NOT NULL,
    input_tokens BIGINT CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens IS NULL OR output_tokens >= 0),
    estimated_cost NUMERIC(18,8)
        CHECK (estimated_cost IS NULL OR estimated_cost >= 0),
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    recorded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_provider_usage_execution
        UNIQUE (run_id, stage, attempt, operation_scope, operation_key, claim_generation),
    CONSTRAINT fk_provider_usage_run_step
        FOREIGN KEY (run_id, stage, attempt)
        REFERENCES run_steps(run_id, stage, attempt)
        ON DELETE RESTRICT,
    CONSTRAINT chk_provider_usage_receipt_state
        CHECK (
            (state = 'STARTED'::provider_usage_state
             AND recorded_at IS NULL
             AND input_tokens IS NULL
             AND output_tokens IS NULL
             AND estimated_cost IS NULL)
            OR
            (state = 'UNAVAILABLE'::provider_usage_state
             AND recorded_at IS NOT NULL
             AND input_tokens IS NULL
             AND output_tokens IS NULL
             AND estimated_cost IS NULL)
            OR
            (state = 'RECORDED'::provider_usage_state
             AND recorded_at IS NOT NULL)
        )
);

CREATE INDEX idx_provider_usage_events_run_step
    ON provider_usage_events(run_id, stage, attempt);

-- READY and FAILED versions are terminal durable history.  Failed input must
-- not be silently rewritten into a new lifecycle under the same content hash.
CREATE OR REPLACE FUNCTION enforce_ready_document_version_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.processing_status IN ('READY'::document_version_status, 'FAILED'::document_version_status)
       AND (
           NEW.document_id IS DISTINCT FROM OLD.document_id
           OR NEW.version_number IS DISTINCT FROM OLD.version_number
           OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
           OR NEW.storage_uri IS DISTINCT FROM OLD.storage_uri
           OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
           OR NEW.size_bytes IS DISTINCT FROM OLD.size_bytes
           OR NEW.processing_status IS DISTINCT FROM OLD.processing_status
       ) THEN
        RAISE EXCEPTION
            '% document version % is immutable',
            OLD.processing_status,
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

-- `latest_run_id` is the automatic-arrival lineage tail.  PostgreSQL can
-- enforce the same-pile identity; the application owns forward-child intent
-- while holding the pile row lock.  Validate historical rows before installing
-- the future-write trigger so pre-existing cross-pile corruption fails the
-- migration rather than being silently accepted.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM piles AS p
        JOIN runs AS r ON r.id = p.latest_run_id
        WHERE p.latest_run_id IS NOT NULL
          AND r.pile_id IS DISTINCT FROM p.id
    ) THEN
        RAISE EXCEPTION
            'existing piles.latest_run_id contains a cross-pile lineage tail';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION validate_pile_latest_run_same_pile()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    tail_pile UUID;
BEGIN
    IF NEW.latest_run_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT pile_id INTO tail_pile FROM runs WHERE id = NEW.latest_run_id;
    IF NOT FOUND OR tail_pile IS DISTINCT FROM NEW.id THEN
        RAISE EXCEPTION 'piles.latest_run_id % must reference a Run in the same pile', NEW.latest_run_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_pile_latest_run_same_pile
BEFORE INSERT OR UPDATE OF latest_run_id ON piles
FOR EACH ROW EXECUTE FUNCTION validate_pile_latest_run_same_pile();

CREATE OR REPLACE FUNCTION prevent_automatic_run_parent_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.parent_run_id IS NOT NULL
       AND NEW.parent_run_id IS DISTINCT FROM OLD.parent_run_id THEN
        RAISE EXCEPTION 'run % parent_run_id is immutable once set', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_prevent_automatic_run_parent_mutation
BEFORE UPDATE OF parent_run_id ON runs
FOR EACH ROW EXECUTE FUNCTION prevent_automatic_run_parent_mutation();

-- A fresh FINAL_REPORT is produced only by a running COMMIT attempt on a
-- commit-required Run. Historical completed rows are untouched because this
-- trigger applies only to new output inserts.
CREATE OR REPLACE FUNCTION require_commit_for_final_report_output()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    current_run_status run_status;
    current_commit_required BOOLEAN;
    commit_status run_step_status;
BEGIN
    IF NEW.output_type <> 'FINAL_REPORT' THEN
        RETURN NEW;
    END IF;

    SELECT status, commit_required
    INTO current_run_status, current_commit_required
    FROM runs
    WHERE id = NEW.run_id
    FOR KEY SHARE;

    IF NOT FOUND OR current_run_status <> 'REVIEW_REQUIRED'::run_status THEN
        RAISE EXCEPTION 'FINAL_REPORT output requires REVIEW_REQUIRED Run';
    END IF;

    IF current_commit_required IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'historical Run is read-only for FINAL_REPORT output';
    END IF;

    SELECT status INTO commit_status
    FROM run_steps
    WHERE run_id = NEW.run_id AND stage = 'COMMIT'::run_stage
    ORDER BY attempt DESC
    LIMIT 1
    FOR KEY SHARE;

    IF commit_status IS DISTINCT FROM 'RUNNING'::run_step_status THEN
        RAISE EXCEPTION 'FINAL_REPORT output requires active COMMIT RunStep';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_require_commit_for_final_report_output
BEFORE INSERT ON outputs
FOR EACH ROW EXECUTE FUNCTION require_commit_for_final_report_output();

-- Every post-cutover completion transition must carry both the canonical final
-- report and a completed latest COMMIT attempt. Historical rows already marked
-- COMPLETED before this migration are not transitioned and remain readable.
CREATE OR REPLACE FUNCTION require_commit_for_run_completion()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    commit_status run_step_status;
BEGIN
    IF NEW.status = 'COMPLETED'::run_status
       AND OLD.status IS DISTINCT FROM 'COMPLETED'::run_status THEN
        -- FALSE exists only on Runs that were already COMPLETED when 006 ran.
        -- This branch is defensive; the cutover-marker trigger prevents callers
        -- from manufacturing or downgrading a Run into that compatibility class.
        IF OLD.commit_required IS DISTINCT FROM TRUE THEN
            RETURN NEW;
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM outputs
            WHERE run_id = NEW.id AND output_type = 'FINAL_REPORT'
        ) THEN
            RAISE EXCEPTION 'Run completion requires FINAL_REPORT output';
        END IF;

        SELECT status INTO commit_status
        FROM run_steps
        WHERE run_id = NEW.id AND stage = 'COMMIT'::run_stage
        ORDER BY attempt DESC
        LIMIT 1
        FOR KEY SHARE;

        IF commit_status IS DISTINCT FROM 'COMPLETED'::run_step_status THEN
            RAISE EXCEPTION 'Run completion requires completed COMMIT RunStep';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_require_commit_for_run_completion
BEFORE UPDATE OF status ON runs
FOR EACH ROW EXECUTE FUNCTION require_commit_for_run_completion();

COMMIT;
