-- ---------------------------------------------------------------------------
-- IMMUTABLE UPDATE CHANGE-SET REVIEW
--
-- This additive migration closes the durable UPDATE-level approval gap. It
-- preserves migrations 001 and 002 unchanged.
-- ---------------------------------------------------------------------------

CREATE TABLE update_change_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL UNIQUE REFERENCES runs(id) ON DELETE RESTRICT,
    baseline_run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    trigger_document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    preparation_fingerprint TEXT NOT NULL
        CHECK (preparation_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    review_state_fingerprint TEXT NOT NULL
        CHECK (review_state_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    render_input_fingerprint TEXT NOT NULL
        CHECK (render_input_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    manifest JSONB NOT NULL,
    change_set_fingerprint TEXT NOT NULL
        CHECK (change_set_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE update_review_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    change_set_id UUID NOT NULL UNIQUE
        REFERENCES update_change_sets(id) ON DELETE RESTRICT,
    decision review_decision_type NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- CHANGE-SET INSERT BACKSTOPS
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_update_change_set_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    run_baseline_id UUID;
    run_trigger_id UUID;
    run_type_value run_type;
    run_status_value run_status;
BEGIN
    SELECT run_type, status, baseline_run_id, trigger_document_version_id
    INTO run_type_value, run_status_value, run_baseline_id, run_trigger_id
    FROM runs
    WHERE id = NEW.run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'update change-set run % does not exist', NEW.run_id;
    END IF;

    IF run_type_value <> 'UPDATE'::run_type
       OR run_status_value <> 'REVIEW_REQUIRED'::run_status THEN
        RAISE EXCEPTION 'update change-set requires a REVIEW_REQUIRED UPDATE run %', NEW.run_id;
    END IF;

    IF NEW.baseline_run_id IS DISTINCT FROM run_baseline_id
       OR NEW.trigger_document_version_id IS DISTINCT FROM run_trigger_id THEN
        RAISE EXCEPTION 'update change-set baseline/trigger must match run %', NEW.run_id;
    END IF;

    IF jsonb_typeof(NEW.manifest) IS DISTINCT FROM 'object'
       OR jsonb_typeof(NEW.manifest -> 'format') IS DISTINCT FROM 'string'
       OR jsonb_typeof(NEW.manifest -> 'run_id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(NEW.manifest -> 'baseline_run_id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(NEW.manifest -> 'trigger_document_version_id') IS DISTINCT FROM 'string'
       OR jsonb_typeof(NEW.manifest -> 'preparation_fingerprint') IS DISTINCT FROM 'string'
       OR jsonb_typeof(NEW.manifest -> 'review_state_fingerprint') IS DISTINCT FROM 'string'
       OR jsonb_typeof(NEW.manifest -> 'render_input_fingerprint') IS DISTINCT FROM 'string'
       OR NEW.manifest ->> 'format' IS DISTINCT FROM 'update-change-set-v1'
       OR NEW.manifest ->> 'run_id' IS DISTINCT FROM NEW.run_id::text
       OR NEW.manifest ->> 'baseline_run_id' IS DISTINCT FROM NEW.baseline_run_id::text
       OR NEW.manifest ->> 'trigger_document_version_id'
            IS DISTINCT FROM NEW.trigger_document_version_id::text
       OR NEW.manifest ->> 'preparation_fingerprint' IS DISTINCT FROM NEW.preparation_fingerprint
       OR NEW.manifest ->> 'review_state_fingerprint' IS DISTINCT FROM NEW.review_state_fingerprint
       OR NEW.manifest ->> 'render_input_fingerprint' IS DISTINCT FROM NEW.render_input_fingerprint THEN
        RAISE EXCEPTION 'update change-set manifest must match durable scalar identity';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_update_change_set_insert
BEFORE INSERT ON update_change_sets
FOR EACH ROW EXECUTE FUNCTION validate_update_change_set_insert();

-- ---------------------------------------------------------------------------
-- REVIEW DECISION INSERT BACKSTOP
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_update_review_decision_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    review_run_id UUID;
    review_run_status run_status;
BEGIN
    SELECT runs.id, runs.status
    INTO review_run_id, review_run_status
    FROM update_change_sets
    JOIN runs ON runs.id = update_change_sets.run_id
    WHERE update_change_sets.id = NEW.change_set_id
    FOR UPDATE OF runs;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'update change-set % does not exist', NEW.change_set_id;
    END IF;

    IF review_run_status <> 'REVIEW_REQUIRED'::run_status THEN
        RAISE EXCEPTION 'update review requires a REVIEW_REQUIRED run %', review_run_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM run_findings
        JOIN findings ON findings.id = run_findings.finding_id
        WHERE run_findings.run_id = review_run_id
          AND findings.status = 'PENDING'::finding_status
    ) THEN
        RAISE EXCEPTION 'update review requires complete finding review for run %', review_run_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_update_review_decision_insert
BEFORE INSERT ON update_review_decisions
FOR EACH ROW EXECUTE FUNCTION validate_update_review_decision_insert();

-- ---------------------------------------------------------------------------
-- IMMUTABILITY
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reject_update_review_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'update review records are immutable';
END;
$$;

CREATE TRIGGER trg_update_change_set_immutable
BEFORE UPDATE OR DELETE ON update_change_sets
FOR EACH ROW EXECUTE FUNCTION reject_update_review_mutation();

CREATE TRIGGER trg_update_review_decision_immutable
BEFORE UPDATE OR DELETE ON update_review_decisions
FOR EACH ROW EXECUTE FUNCTION reject_update_review_mutation();
