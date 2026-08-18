-- ---------------------------------------------------------------------------
-- ITEM-LEVEL UPDATE REVIEW
--
-- Adds durable per-Fact UPDATE review items while preserving the legacy
-- whole-change-set decision for UPDATEs that have no material Fact delta.
-- Existing itemized legacy decisions are backfilled item-by-item.
-- ---------------------------------------------------------------------------

BEGIN;

CREATE TYPE update_change_item_kind AS ENUM (
    'FACT_CREATED',
    'FACT_REMOVED'
);

CREATE TABLE update_change_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    change_set_id UUID NOT NULL
        REFERENCES update_change_sets(id) ON DELETE RESTRICT,
    fact_id UUID NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
    kind update_change_item_kind NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (change_set_id, fact_id)
);

CREATE TABLE update_item_review_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL UNIQUE
        REFERENCES update_change_items(id) ON DELETE RESTRICT,
    decision review_decision_type NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Materialize item rows for all pre-existing UPDATE change sets.
INSERT INTO update_change_items (change_set_id, fact_id, kind, payload, created_at)
SELECT
    change_set.id,
    (delta ->> 'fact_id')::uuid,
    CASE delta ->> 'kind'
        WHEN 'CREATED' THEN 'FACT_CREATED'::update_change_item_kind
        WHEN 'REMOVED_FROM_CURRENT_STATE' THEN 'FACT_REMOVED'::update_change_item_kind
    END,
    delta,
    change_set.created_at
FROM update_change_sets AS change_set
CROSS JOIN LATERAL jsonb_array_elements(change_set.manifest -> 'fact_delta') AS delta
WHERE delta ->> 'kind' IN ('CREATED', 'REMOVED_FROM_CURRENT_STATE');

-- Preserve historical whole-change-set decisions by projecting them onto each
-- material item. New itemized UPDATEs may no longer use a whole-set decision.
INSERT INTO update_item_review_decisions (
    item_id,
    decision,
    actor_type,
    actor_id,
    idempotency_key,
    decided_at
)
SELECT
    item.id,
    legacy.decision,
    legacy.actor_type,
    legacy.actor_id,
    legacy.idempotency_key || ':item:' || item.fact_id::text,
    legacy.decided_at
FROM update_review_decisions AS legacy
JOIN update_change_items AS item ON item.change_set_id = legacy.change_set_id;

CREATE OR REPLACE FUNCTION materialize_update_change_items()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    delta JSONB;
    delta_kind TEXT;
BEGIN
    IF jsonb_typeof(NEW.manifest -> 'fact_delta') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'update change-set fact_delta must be an array';
    END IF;

    FOR delta IN SELECT value FROM jsonb_array_elements(NEW.manifest -> 'fact_delta')
    LOOP
        delta_kind := delta ->> 'kind';
        IF delta_kind IN ('CREATED', 'REMOVED_FROM_CURRENT_STATE') THEN
            IF jsonb_typeof(delta) IS DISTINCT FROM 'object'
               OR jsonb_typeof(delta -> 'fact_id') IS DISTINCT FROM 'string' THEN
                RAISE EXCEPTION 'reviewable update fact delta is malformed';
            END IF;
            INSERT INTO update_change_items (change_set_id, fact_id, kind, payload)
            VALUES (
                NEW.id,
                (delta ->> 'fact_id')::uuid,
                CASE delta_kind
                    WHEN 'CREATED' THEN 'FACT_CREATED'::update_change_item_kind
                    ELSE 'FACT_REMOVED'::update_change_item_kind
                END,
                delta
            );
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_materialize_update_change_items
AFTER INSERT ON update_change_sets
FOR EACH ROW EXECUTE FUNCTION materialize_update_change_items();

CREATE OR REPLACE FUNCTION validate_update_change_item_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    expected_delta_kind TEXT;
    parent_manifest JSONB;
BEGIN
    SELECT manifest
    INTO parent_manifest
    FROM update_change_sets
    WHERE id = NEW.change_set_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'update change-set % does not exist', NEW.change_set_id;
    END IF;

    expected_delta_kind := CASE NEW.kind
        WHEN 'FACT_CREATED'::update_change_item_kind THEN 'CREATED'
        WHEN 'FACT_REMOVED'::update_change_item_kind THEN 'REMOVED_FROM_CURRENT_STATE'
    END;

    IF jsonb_typeof(NEW.payload) IS DISTINCT FROM 'object'
       OR NEW.payload ->> 'fact_id' IS DISTINCT FROM NEW.fact_id::text
       OR NEW.payload ->> 'kind' IS DISTINCT FROM expected_delta_kind
       OR NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(parent_manifest -> 'fact_delta') AS delta
            WHERE delta = NEW.payload
       ) THEN
        RAISE EXCEPTION 'update change item must match one material Fact delta in its change set';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_update_change_item_insert
BEFORE INSERT ON update_change_items
FOR EACH ROW EXECUTE FUNCTION validate_update_change_item_insert();

CREATE OR REPLACE FUNCTION validate_update_item_review_decision_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    review_run_id UUID;
    review_run_status run_status;
BEGIN
    SELECT runs.id, runs.status
    INTO review_run_id, review_run_status
    FROM update_change_items
    JOIN update_change_sets ON update_change_sets.id = update_change_items.change_set_id
    JOIN runs ON runs.id = update_change_sets.run_id
    WHERE update_change_items.id = NEW.item_id
    FOR UPDATE OF runs;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'update change item % does not exist', NEW.item_id;
    END IF;

    IF review_run_status <> 'REVIEW_REQUIRED'::run_status THEN
        RAISE EXCEPTION 'update item review requires a REVIEW_REQUIRED run %', review_run_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM run_findings
        JOIN findings ON findings.id = run_findings.finding_id
        WHERE run_findings.run_id = review_run_id
          AND findings.status = 'PENDING'::finding_status
    ) THEN
        RAISE EXCEPTION 'update item review requires complete finding review for run %', review_run_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_update_item_review_decision_insert
BEFORE INSERT ON update_item_review_decisions
FOR EACH ROW EXECUTE FUNCTION validate_update_item_review_decision_insert();

-- A material UPDATE cannot bypass item-level review with one whole-set approval.
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

    IF EXISTS (
        SELECT 1
        FROM update_change_items
        WHERE change_set_id = NEW.change_set_id
    ) THEN
        RAISE EXCEPTION 'itemized UPDATE change set % requires item-level review', NEW.change_set_id;
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

CREATE OR REPLACE FUNCTION reject_update_item_review_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'update item review records are immutable';
END;
$$;

CREATE TRIGGER trg_update_change_item_immutable
BEFORE UPDATE OR DELETE ON update_change_items
FOR EACH ROW EXECUTE FUNCTION reject_update_item_review_mutation();

CREATE TRIGGER trg_update_item_review_decision_immutable
BEFORE UPDATE OR DELETE ON update_item_review_decisions
FOR EACH ROW EXECUTE FUNCTION reject_update_item_review_mutation();

COMMIT;
