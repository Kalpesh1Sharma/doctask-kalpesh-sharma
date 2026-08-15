-- Phase 5B: durable generation fence for stale idempotency takeover.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM idempotency_records
        WHERE status = 'IN_PROGRESS'::idempotency_status
          AND claimed_at IS NULL
    ) THEN
        RAISE EXCEPTION
            'cannot add idempotency claim fence: IN_PROGRESS record has NULL claimed_at';
    END IF;
END $$;

ALTER TABLE idempotency_records
    ADD COLUMN claim_generation BIGINT;

UPDATE idempotency_records
SET claim_generation = 0
WHERE claim_generation IS NULL;

ALTER TABLE idempotency_records
    ALTER COLUMN claim_generation SET NOT NULL,
    ALTER COLUMN claim_generation SET DEFAULT 1,
    ADD CONSTRAINT chk_idempotency_claim_generation_nonnegative
        CHECK (claim_generation >= 0),
    ADD CONSTRAINT chk_idempotency_in_progress_claimed_at
        CHECK (
            status <> 'IN_PROGRESS'::idempotency_status
            OR claimed_at IS NOT NULL
        );
