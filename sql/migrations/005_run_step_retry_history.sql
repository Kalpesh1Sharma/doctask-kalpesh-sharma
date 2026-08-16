-- Phase 5C: explicit semantic retry attempts, durable failure reason, and metrics.

CREATE TYPE idempotency_failure_kind AS ENUM (
    'RETRYABLE_PROVIDER_FAILURE',
    'RETRYABLE_INVALID_RESPONSE',
    'RETRYABLE_EVIDENCE_VALIDATION',
    'RETRYABLE_UNSUPPORTED_EVALUATION'
);

ALTER TABLE idempotency_records
    ADD COLUMN failure_kind idempotency_failure_kind,
    ADD CONSTRAINT chk_idempotency_failure_kind_terminal
        CHECK (status = 'FAILED'::idempotency_status OR failure_kind IS NULL);

ALTER TABLE run_steps
    ADD COLUMN retry_operation_scope TEXT,
    ADD COLUMN retry_operation_key TEXT,
    ADD COLUMN retry_from_claim_generation BIGINT,
    ADD CONSTRAINT chk_run_step_attempt_limit CHECK (attempt <= 3),
    ADD CONSTRAINT chk_run_step_retry_provenance
        CHECK (
            (attempt = 1
             AND retry_operation_scope IS NULL
             AND retry_operation_key IS NULL
             AND retry_from_claim_generation IS NULL)
            OR
            (attempt > 1
             AND retry_operation_scope IS NOT NULL
             AND retry_operation_scope <> ''
             AND retry_operation_key IS NOT NULL
             AND retry_operation_key <> ''
             AND retry_from_claim_generation IS NOT NULL
             AND retry_from_claim_generation >= 0)
        ),
    ADD CONSTRAINT chk_closed_run_step_timestamps
        CHECK (
            status NOT IN (
                'COMPLETED'::run_step_status,
                'RETRYING'::run_step_status,
                'SKIPPED'::run_step_status,
                'ESCALATED'::run_step_status,
                'FAILED'::run_step_status
            )
            OR (
                started_at IS NOT NULL
                AND completed_at IS NOT NULL
                AND completed_at >= started_at
            )
        ),
    ADD CONSTRAINT chk_retrying_attempt_limit
        CHECK (status <> 'RETRYING'::run_step_status OR attempt < 3);

DO $$
DECLARE
    invalid_metric BOOLEAN;
    invalid_closed_step BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM run_metrics metric
        LEFT JOIN run_steps step
          ON step.run_id = metric.run_id
         AND step.stage = metric.stage
         AND step.attempt = metric.attempt
        WHERE step.id IS NULL
           OR step.status NOT IN (
                'COMPLETED'::run_step_status,
                'RETRYING'::run_step_status,
                'SKIPPED'::run_step_status,
                'ESCALATED'::run_step_status,
                'FAILED'::run_step_status
           )
    ) INTO invalid_metric;
    IF invalid_metric THEN
        RAISE EXCEPTION 'cannot add retry metric integrity: metric has no closed RunStep';
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM run_steps
        WHERE status IN (
            'COMPLETED'::run_step_status,
            'RETRYING'::run_step_status,
            'SKIPPED'::run_step_status,
            'ESCALATED'::run_step_status,
            'FAILED'::run_step_status
        )
          AND (started_at IS NULL OR completed_at IS NULL OR completed_at < started_at)
    ) INTO invalid_closed_step;
    IF invalid_closed_step THEN
        RAISE EXCEPTION 'cannot add retry metric integrity: closed RunStep has invalid timestamps';
    END IF;
END $$;

DO $$
DECLARE
    mismatch BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1
        FROM run_metrics metric
        JOIN run_steps step
          ON step.run_id = metric.run_id
         AND step.stage = metric.stage
         AND step.attempt = metric.attempt
        WHERE metric.duration_ms IS NOT NULL
          AND metric.duration_ms <> FLOOR(
              EXTRACT(EPOCH FROM (step.completed_at - step.started_at)) * 1000
          )::BIGINT
    ) INTO mismatch;
    IF mismatch THEN
        RAISE EXCEPTION 'cannot add retry metric integrity: historical duration differs from timestamps';
    END IF;
END $$;

UPDATE run_metrics metric
SET duration_ms = FLOOR(
    EXTRACT(EPOCH FROM (step.completed_at - step.started_at)) * 1000
)::BIGINT
FROM run_steps step
WHERE step.run_id = metric.run_id
  AND step.stage = metric.stage
  AND step.attempt = metric.attempt
  AND metric.duration_ms IS NULL;

INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
SELECT step.run_id, step.stage, step.attempt,
       FLOOR(EXTRACT(EPOCH FROM (step.completed_at - step.started_at)) * 1000)::BIGINT
FROM run_steps step
LEFT JOIN run_metrics metric
  ON metric.run_id = step.run_id
 AND metric.stage = step.stage
 AND metric.attempt = step.attempt
WHERE metric.id IS NULL
  AND step.status IN (
      'COMPLETED'::run_step_status,
      'RETRYING'::run_step_status,
      'SKIPPED'::run_step_status,
      'ESCALATED'::run_step_status,
      'FAILED'::run_step_status
  );

ALTER TABLE run_metrics
    ALTER COLUMN duration_ms SET NOT NULL,
    ADD CONSTRAINT fk_run_metrics_run_step
        FOREIGN KEY (run_id, stage, attempt)
        REFERENCES run_steps(run_id, stage, attempt)
        ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION validate_run_step_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'PENDING' AND NEW.status = 'RUNNING' THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'RUNNING' AND NEW.status IN (
        'COMPLETED', 'RETRYING', 'SKIPPED', 'ESCALATED', 'FAILED', 'AWAITING_UPSTREAM'
    ) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'AWAITING_UPSTREAM' AND NEW.status = 'RUNNING' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid run_step transition: % -> %', OLD.status, NEW.status;
END;
$$;

CREATE OR REPLACE FUNCTION validate_run_metric_closed_step()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    step_status run_step_status;
BEGIN
    SELECT status INTO step_status
    FROM run_steps
    WHERE run_id = NEW.run_id AND stage = NEW.stage AND attempt = NEW.attempt;
    IF step_status NOT IN ('COMPLETED', 'RETRYING', 'SKIPPED', 'ESCALATED', 'FAILED') THEN
        RAISE EXCEPTION 'run_metric must reference a closed RunStep';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_run_metric_closed_step
BEFORE INSERT OR UPDATE OF run_id, stage, attempt ON run_metrics
FOR EACH ROW EXECUTE FUNCTION validate_run_metric_closed_step();

CREATE OR REPLACE FUNCTION validate_run_step_retry_history()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    predecessor run_steps%ROWTYPE;
    operation idempotency_records%ROWTYPE;
BEGIN
    IF NEW.attempt > 1 THEN
        SELECT * INTO predecessor
        FROM run_steps
        WHERE run_id = NEW.run_id AND stage = NEW.stage AND attempt = NEW.attempt - 1;
        IF NOT FOUND OR predecessor.status <> 'RETRYING' THEN
            RAISE EXCEPTION 'retry successor requires contiguous RETRYING predecessor';
        END IF;
        SELECT * INTO operation
        FROM idempotency_records
        WHERE scope = NEW.retry_operation_scope AND key = NEW.retry_operation_key;
        IF NOT FOUND
           OR operation.status <> 'FAILED'
           OR operation.claim_generation <> NEW.retry_from_claim_generation
           OR operation.failure_kind IS NULL THEN
            RAISE EXCEPTION 'retry successor provenance does not reference retryable failed operation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_run_step_retry_successor
BEFORE INSERT ON run_steps
FOR EACH ROW EXECUTE FUNCTION validate_run_step_retry_history();

CREATE OR REPLACE FUNCTION prevent_retried_step_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM run_steps successor
        WHERE successor.run_id = OLD.run_id
          AND successor.stage = OLD.stage
          AND successor.attempt = OLD.attempt + 1
    ) THEN
        RAISE EXCEPTION 'historical retried RunStep is immutable';
    END IF;
    IF TG_OP = 'DELETE'
       AND OLD.attempt > 1
       AND EXISTS (
           SELECT 1 FROM run_steps predecessor
           WHERE predecessor.run_id = OLD.run_id
             AND predecessor.stage = OLD.stage
             AND predecessor.attempt = OLD.attempt - 1
             AND predecessor.status = 'RETRYING'::run_step_status
       ) THEN
        RAISE EXCEPTION 'retry successor cannot be removed while predecessor is RETRYING';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_prevent_retried_step_mutation
BEFORE UPDATE OR DELETE ON run_steps
FOR EACH ROW EXECUTE FUNCTION prevent_retried_step_mutation();

CREATE OR REPLACE FUNCTION require_closed_step_metric()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status IN ('COMPLETED', 'RETRYING', 'SKIPPED', 'ESCALATED', 'FAILED')
       AND NOT EXISTS (
           SELECT 1 FROM run_metrics
           WHERE run_id = NEW.run_id AND stage = NEW.stage AND attempt = NEW.attempt
       ) THEN
        RAISE EXCEPTION 'closed RunStep requires exactly one metric';
    END IF;
    IF NEW.status = 'RETRYING'
       AND NOT EXISTS (
           SELECT 1 FROM run_steps successor
           WHERE successor.run_id = NEW.run_id
             AND successor.stage = NEW.stage
             AND successor.attempt = NEW.attempt + 1
       ) THEN
        RAISE EXCEPTION 'RETRYING RunStep requires contiguous successor';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_require_closed_step_metric
AFTER INSERT OR UPDATE OF status ON run_steps
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_closed_step_metric();
