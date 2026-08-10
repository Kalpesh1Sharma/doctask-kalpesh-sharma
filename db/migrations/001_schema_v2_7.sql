-- Schema V2.7 — PostgreSQL DDL
-- Locked schema contract; implementation notes N1–N3 are handled below.
--
-- Assumptions:
--   * PostgreSQL 15+.
--   * pgvector is installed and available.
--   * UUIDs are used for identifiers.
--   * Embedding retrieval uses cosine distance.
--   * This revision fixes the embedding dimension at 1536, matching the
--     assumed OpenAI text-embedding-3-small model. If the application uses
--     another embedding model, change vector(1536) to that model's dimension
--     before applying the migration.
--
-- IMPORTANT APPLICATION TRANSACTIONS:
--   * document version creation/current_version advancement is serialized
--     by SELECT ... FOR UPDATE on documents.
--   * INITIAL/UPDATE chain assignment is serialized by SELECT ... FOR UPDATE
--     on piles.
--   * approval/rejection persists decision, completeness check and commit
--     application in one transaction with SELECT ... FOR UPDATE on runs.
--   * LLM idempotency claim and provider call are NOT held inside one DB tx.
--
-- N1: run_document_versions is populated incrementally as input scope is
--     discovered, with the relevant document-result/idempotency transaction.
-- N2: AWAITING_UPSTREAM wake/sweep uses current DB state; redundant wake
--     attempts are application no-ops rather than invalid state transitions.
-- N3: HNSW uses vector_cosine_ops because retrieval is assumed to use cosine.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- ENUM TYPES
-- ---------------------------------------------------------------------------

CREATE TYPE document_version_status AS ENUM ('PENDING', 'READY', 'FAILED');

CREATE TYPE run_type AS ENUM ('INITIAL', 'UPDATE', 'MANUAL');

CREATE TYPE run_status AS ENUM (
    'PENDING',
    'RUNNING',
    'REVIEW_REQUIRED',
    'COMPLETED',
    'FAILED'
);

CREATE TYPE run_stage AS ENUM (
    'INGEST',
    'UNDERSTAND',
    'EXAMINE',
    'REVIEW',
    'COMMIT'
);

CREATE TYPE run_step_status AS ENUM (
    'PENDING',
    'RUNNING',
    'AWAITING_UPSTREAM',
    'COMPLETED',
    'RETRYING',
    'SKIPPED',
    'ESCALATED',
    'FAILED'
);

CREATE TYPE fact_status AS ENUM (
    'CONFIRMED',
    'DISPUTED',
    'SUPERSEDED'
);

CREATE TYPE run_membership_relation AS ENUM (
    'CREATED',
    'CARRIED_FORWARD'
);

CREATE TYPE finding_type AS ENUM (
    'RULE_VIOLATION',
    'CONTRADICTION'
);

CREATE TYPE finding_status AS ENUM (
    'PENDING',
    'APPROVED',
    'REJECTED'
);

CREATE TYPE review_decision_type AS ENUM (
    'APPROVED',
    'REJECTED'
);

CREATE TYPE idempotency_status AS ENUM (
    'IN_PROGRESS',
    'COMPLETED',
    'FAILED'
);

-- ---------------------------------------------------------------------------
-- PILES
-- ---------------------------------------------------------------------------

CREATE TABLE piles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    watch_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    watch_location TEXT,
    latest_run_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- DOCUMENTS
-- current_version_id is added as an FK after document_versions exists.
-- ---------------------------------------------------------------------------

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id UUID NOT NULL REFERENCES piles(id) ON DELETE RESTRICT,
    logical_key TEXT NOT NULL,
    current_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_documents_pile_logical_key
        UNIQUE (pile_id, logical_key)
);

-- ---------------------------------------------------------------------------
-- DOCUMENT VERSIONS
-- ---------------------------------------------------------------------------

CREATE TABLE document_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
    version_number BIGINT NOT NULL CHECK (version_number > 0),
    content_hash TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT CHECK (size_bytes >= 0),
    processing_status document_version_status NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_document_version_number
        UNIQUE (document_id, version_number),

    CONSTRAINT uq_document_content_hash
        UNIQUE (document_id, content_hash)
);

ALTER TABLE documents
    ADD CONSTRAINT fk_documents_current_version
    FOREIGN KEY (current_version_id)
    REFERENCES document_versions(id)
    ON DELETE RESTRICT;

-- ---------------------------------------------------------------------------
-- DOCUMENT CHUNKS
-- ---------------------------------------------------------------------------

CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    page_number INTEGER CHECK (page_number IS NULL OR page_number > 0),
    start_offset BIGINT CHECK (start_offset IS NULL OR start_offset >= 0),
    end_offset BIGINT CHECK (end_offset IS NULL OR end_offset >= 0),
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_chunk_version_index
        UNIQUE (document_version_id, chunk_index),

    -- Required for the composite evidence FK.
    CONSTRAINT uq_chunk_id_version
        UNIQUE (id, document_version_id),

    CONSTRAINT chk_chunk_offsets
        CHECK (
            start_offset IS NULL
            OR end_offset IS NULL
            OR end_offset >= start_offset
        )
);

-- ---------------------------------------------------------------------------
-- EVIDENCE SPANS
-- ---------------------------------------------------------------------------

CREATE TABLE evidence_spans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    chunk_id UUID NOT NULL,
    page_number INTEGER CHECK (page_number IS NULL OR page_number > 0),
    start_offset BIGINT CHECK (start_offset IS NULL OR start_offset >= 0),
    end_offset BIGINT CHECK (end_offset IS NULL OR end_offset >= 0),
    quoted_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT fk_evidence_chunk_same_version
        FOREIGN KEY (chunk_id, document_version_id)
        REFERENCES document_chunks(id, document_version_id)
        ON DELETE RESTRICT,

    CONSTRAINT chk_evidence_offsets
        CHECK (
            start_offset IS NULL
            OR end_offset IS NULL
            OR end_offset >= start_offset
        )
);

-- ---------------------------------------------------------------------------
-- RULE SETS / RULES
-- ---------------------------------------------------------------------------

CREATE TABLE rule_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id UUID NOT NULL REFERENCES piles(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    description TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_set_id UUID NOT NULL REFERENCES rule_sets(id) ON DELETE RESTRICT,
    rule_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    severity TEXT NOT NULL,
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_ruleset_rule_key
        UNIQUE (rule_set_id, rule_key)
);

-- ---------------------------------------------------------------------------
-- RUNS
-- ---------------------------------------------------------------------------

CREATE TABLE runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id UUID NOT NULL REFERENCES piles(id) ON DELETE RESTRICT,
    run_type run_type NOT NULL,
    status run_status NOT NULL DEFAULT 'PENDING',
    trigger_document_version_id UUID
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    parent_run_id UUID
        REFERENCES runs(id) ON DELETE RESTRICT,
    baseline_run_id UUID
        REFERENCES runs(id) ON DELETE RESTRICT,
    blocked_on_run_id UUID
        REFERENCES runs(id) ON DELETE RESTRICT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_update_trigger
        CHECK (
            (run_type = 'UPDATE' AND trigger_document_version_id IS NOT NULL)
            OR
            (run_type IN ('INITIAL', 'MANUAL')
             AND trigger_document_version_id IS NULL)
        ),

    CONSTRAINT chk_baseline_blocker_exclusive
        CHECK (
            baseline_run_id IS NULL
            OR blocked_on_run_id IS NULL
        ),

    CONSTRAINT chk_run_completion_timestamp
        CHECK (
            status <> 'COMPLETED'
            OR completed_at IS NOT NULL
        )
);

ALTER TABLE piles
    ADD CONSTRAINT fk_piles_latest_run
    FOREIGN KEY (latest_run_id)
    REFERENCES runs(id)
    ON DELETE RESTRICT;

-- Only one UPDATE run may claim a particular trigger document version.
CREATE UNIQUE INDEX uq_update_trigger_document_version
    ON runs (trigger_document_version_id)
    WHERE run_type = 'UPDATE';

-- Database backstop: at most one INITIAL run per pile.
CREATE UNIQUE INDEX uq_initial_run_per_pile
    ON runs (pile_id)
    WHERE run_type = 'INITIAL';

-- ---------------------------------------------------------------------------
-- RUN DOCUMENT VERSIONS
-- ---------------------------------------------------------------------------

CREATE TABLE run_document_versions (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    document_version_id UUID NOT NULL
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, document_version_id)
);

-- ---------------------------------------------------------------------------
-- RUN STEPS
-- ---------------------------------------------------------------------------

CREATE TABLE run_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    stage run_stage NOT NULL,
    status run_step_status NOT NULL DEFAULT 'PENDING',
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_code TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_run_step_attempt
        UNIQUE (run_id, stage, attempt)
);

-- ---------------------------------------------------------------------------
-- FACTS
-- ---------------------------------------------------------------------------

CREATE TABLE facts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_value JSONB NOT NULL,
    status fact_status NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE run_facts (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    fact_id UUID NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
    relation run_membership_relation NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, fact_id)
);

CREATE TABLE fact_evidence (
    fact_id UUID NOT NULL REFERENCES facts(id) ON DELETE RESTRICT,
    evidence_span_id UUID NOT NULL
        REFERENCES evidence_spans(id) ON DELETE RESTRICT,

    PRIMARY KEY (fact_id, evidence_span_id)
);

-- ---------------------------------------------------------------------------
-- FINDINGS
-- ---------------------------------------------------------------------------

CREATE TABLE findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    rule_id UUID REFERENCES rules(id) ON DELETE RESTRICT,
    finding_type finding_type NOT NULL,
    status finding_status NOT NULL DEFAULT 'PENDING',
    title TEXT NOT NULL,
    description TEXT,
    severity TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_finding_rule_relationship
        CHECK (
            (finding_type = 'RULE_VIOLATION' AND rule_id IS NOT NULL)
            OR
            (finding_type = 'CONTRADICTION' AND rule_id IS NULL)
        )
);

CREATE TABLE run_findings (
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    finding_id UUID NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
    relation run_membership_relation NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, finding_id)
);

CREATE TABLE finding_evidence (
    finding_id UUID NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
    evidence_span_id UUID NOT NULL
        REFERENCES evidence_spans(id) ON DELETE RESTRICT,

    PRIMARY KEY (finding_id, evidence_span_id)
);

-- ---------------------------------------------------------------------------
-- REVIEW DECISIONS
-- ---------------------------------------------------------------------------

CREATE TABLE review_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id UUID NOT NULL REFERENCES findings(id) ON DELETE RESTRICT,
    decision review_decision_type NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idempotency_key TEXT NOT NULL,

    CONSTRAINT uq_review_decision_idempotency
        UNIQUE (idempotency_key)
);

-- ---------------------------------------------------------------------------
-- OUTPUTS
-- ---------------------------------------------------------------------------

CREATE TABLE outputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    output_type TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_run_output_type
        UNIQUE (run_id, output_type)
);

-- ---------------------------------------------------------------------------
-- IDEMPOTENCY RECORDS
-- ---------------------------------------------------------------------------

CREATE TABLE idempotency_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    status idempotency_status NOT NULL,
    result_reference TEXT,
    claimed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,

    CONSTRAINT uq_idempotency_scope_key
        UNIQUE (scope, key)
);

-- ---------------------------------------------------------------------------
-- AUDIT
-- ---------------------------------------------------------------------------

CREATE TABLE audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- METRICS
-- ---------------------------------------------------------------------------

CREATE TABLE run_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    stage run_stage NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    duration_ms BIGINT CHECK (duration_ms IS NULL OR duration_ms >= 0),
    input_tokens BIGINT CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens IS NULL OR output_tokens >= 0),
    estimated_cost NUMERIC(18,8)
        CHECK (estimated_cost IS NULL OR estimated_cost >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_run_metric_attempt
        UNIQUE (run_id, stage, attempt)
);

-- ---------------------------------------------------------------------------
-- GENERIC updated_at TRIGGER
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_piles_updated_at
BEFORE UPDATE ON piles
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_documents_updated_at
BEFORE UPDATE ON documents
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_runs_updated_at
BEFORE UPDATE ON runs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER trg_findings_updated_at
BEFORE UPDATE ON findings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- DOCUMENT CURRENT-VERSION PROTECTION
--
-- current_version_id may only reference a READY version belonging to the
-- same document and may only move forward.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_current_document_version()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    old_version_number BIGINT;
    new_version_number BIGINT;
    new_version_document UUID;
    new_version_status document_version_status;
BEGIN
    IF NEW.current_version_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT
        version_number,
        document_id,
        processing_status
    INTO
        new_version_number,
        new_version_document,
        new_version_status
    FROM document_versions
    WHERE id = NEW.current_version_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'current_version_id % does not exist',
            NEW.current_version_id;
    END IF;

    IF new_version_document <> NEW.id THEN
        RAISE EXCEPTION
            'current_version_id % belongs to document %, not %',
            NEW.current_version_id,
            new_version_document,
            NEW.id;
    END IF;

    IF new_version_status <> 'READY' THEN
        RAISE EXCEPTION
            'current_version_id % must reference a READY version',
            NEW.current_version_id;
    END IF;

    IF OLD.current_version_id IS NOT NULL THEN
        SELECT version_number
        INTO old_version_number
        FROM document_versions
        WHERE id = OLD.current_version_id;

        IF new_version_number <= old_version_number THEN
            RAISE EXCEPTION
                'current_version_id cannot move backwards';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_current_document_version
BEFORE UPDATE OF current_version_id ON documents
FOR EACH ROW
EXECUTE FUNCTION validate_current_document_version();

-- ---------------------------------------------------------------------------
-- READY DOCUMENT VERSION IMMUTABILITY
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_ready_document_version_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.processing_status = 'READY'
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
            'READY document version % is immutable',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_ready_document_version_immutability
BEFORE UPDATE ON document_versions
FOR EACH ROW
EXECUTE FUNCTION enforce_ready_document_version_immutability();

-- ---------------------------------------------------------------------------
-- RUN LINEAGE / BASELINE VALIDATION
--
-- Validates:
--   * baseline_run_id references a COMPLETED run
--   * baseline/blocker are same-pile
--   * parent/blocker are same-pile
--   * baseline/blocker exclusivity is also backed by CHECK above
--
-- blocked_on_run_id is intentionally NOT required to be non-terminal here,
-- because the V2.7 lock review explicitly accepts the check-then-write race
-- and relies on reconciliation sweep for eventual correction.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_run_lineage()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    parent_pile UUID;
    baseline_pile UUID;
    baseline_status run_status;
    blocked_pile UUID;
BEGIN
    IF NEW.parent_run_id IS NOT NULL THEN
        SELECT pile_id INTO parent_pile
        FROM runs
        WHERE id = NEW.parent_run_id;

        IF NOT FOUND OR parent_pile <> NEW.pile_id THEN
            RAISE EXCEPTION
                'parent_run_id % must reference a run in the same pile',
                NEW.parent_run_id;
        END IF;
    END IF;

    IF NEW.baseline_run_id IS NOT NULL THEN
        SELECT pile_id, status
        INTO baseline_pile, baseline_status
        FROM runs
        WHERE id = NEW.baseline_run_id;

        IF NOT FOUND OR baseline_pile <> NEW.pile_id THEN
            RAISE EXCEPTION
                'baseline_run_id % must reference a run in the same pile',
                NEW.baseline_run_id;
        END IF;

        IF baseline_status <> 'COMPLETED' THEN
            RAISE EXCEPTION
                'baseline_run_id % must reference a COMPLETED run',
                NEW.baseline_run_id;
        END IF;

        IF NEW.blocked_on_run_id IS NOT NULL THEN
            RAISE EXCEPTION
                'baseline_run_id and blocked_on_run_id cannot both be set';
        END IF;
    END IF;

    IF NEW.blocked_on_run_id IS NOT NULL THEN
        SELECT pile_id INTO blocked_pile
        FROM runs
        WHERE id = NEW.blocked_on_run_id;

        IF NOT FOUND OR blocked_pile <> NEW.pile_id THEN
            RAISE EXCEPTION
                'blocked_on_run_id % must reference a run in the same pile',
                NEW.blocked_on_run_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_run_lineage
BEFORE INSERT OR UPDATE OF parent_run_id, baseline_run_id, blocked_on_run_id
ON runs
FOR EACH ROW
EXECUTE FUNCTION validate_run_lineage();

-- ---------------------------------------------------------------------------
-- RUN STEP STATE MACHINE
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_run_step_transition()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'PENDING'
       AND NEW.status = 'RUNNING' THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'RUNNING'
       AND NEW.status IN (
           'COMPLETED',
           'RETRYING',
           'SKIPPED',
           'ESCALATED',
           'FAILED',
           'AWAITING_UPSTREAM'
       ) THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'AWAITING_UPSTREAM'
       AND NEW.status = 'RUNNING' THEN
        RETURN NEW;
    END IF;

    IF OLD.status = 'RETRYING'
       AND NEW.status = 'RUNNING' THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        'invalid run_step transition: % -> %',
        OLD.status,
        NEW.status;
END;
$$;

CREATE TRIGGER trg_validate_run_step_transition
BEFORE UPDATE OF status ON run_steps
FOR EACH ROW
EXECUTE FUNCTION validate_run_step_transition();

-- ---------------------------------------------------------------------------
-- FINDING DECISION IMMUTABILITY
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_finding_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.origin_run_id <> OLD.origin_run_id
       OR NEW.rule_id IS DISTINCT FROM OLD.rule_id
       OR NEW.finding_type <> OLD.finding_type
       OR NEW.title <> OLD.title
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.severity <> OLD.severity THEN
        RAISE EXCEPTION
            'finding content is immutable after creation: %',
            OLD.id;
    END IF;

    IF OLD.status IN ('APPROVED', 'REJECTED')
       AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION
            'terminal finding decision cannot change: % -> %',
            OLD.status,
            NEW.status;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_finding_immutability
BEFORE UPDATE ON findings
FOR EACH ROW
EXECUTE FUNCTION enforce_finding_immutability();

-- ---------------------------------------------------------------------------
-- FACT STATUS IMMUTABILITY
--
-- Fact rows are treated as immutable historical records in this DDL:
-- content and status cannot be rewritten after creation. New factual state
-- is represented by new fact rows/membership rather than mutating history.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_fact_immutability()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.origin_run_id <> OLD.origin_run_id
       OR NEW.subject <> OLD.subject
       OR NEW.predicate <> OLD.predicate
       OR NEW.object_value IS DISTINCT FROM OLD.object_value
       OR NEW.status <> OLD.status THEN
        RAISE EXCEPTION
            'fact content/status is immutable after creation: %',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fact_immutability
BEFORE UPDATE ON facts
FOR EACH ROW
EXECUTE FUNCTION enforce_fact_immutability();

-- ---------------------------------------------------------------------------
-- OPTIONAL RUN STATUS GUARD
--
-- Terminal COMPLETED/FAILED runs must not be moved back into an active
-- lifecycle. This supports baseline correctness because a COMPLETED run
-- remains completed once it can be used as an analytical baseline.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION enforce_run_terminal_status()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.status = 'COMPLETED' AND NEW.status <> 'COMPLETED' THEN
        RAISE EXCEPTION
            'COMPLETED run % cannot change status',
            OLD.id;
    END IF;

    IF OLD.status = 'FAILED' AND NEW.status <> 'FAILED' THEN
        RAISE EXCEPTION
            'FAILED run % cannot change status',
            OLD.id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_run_terminal_status
BEFORE UPDATE OF status ON runs
FOR EACH ROW
EXECUTE FUNCTION enforce_run_terminal_status();

-- ---------------------------------------------------------------------------
-- INDEXES FOR CORE QUERY PATHS
-- ---------------------------------------------------------------------------

CREATE INDEX idx_documents_pile
    ON documents(pile_id);

CREATE INDEX idx_document_versions_document
    ON document_versions(document_id);

CREATE INDEX idx_document_chunks_version
    ON document_chunks(document_version_id);

CREATE INDEX idx_evidence_spans_version
    ON evidence_spans(document_version_id);

CREATE INDEX idx_rule_sets_pile
    ON rule_sets(pile_id);

CREATE INDEX idx_rules_rule_set
    ON rules(rule_set_id);

CREATE INDEX idx_runs_pile_created
    ON runs(pile_id, created_at);

CREATE INDEX idx_runs_parent
    ON runs(parent_run_id);

CREATE INDEX idx_runs_baseline
    ON runs(baseline_run_id);

CREATE INDEX idx_runs_blocked_on
    ON runs(blocked_on_run_id)
    WHERE blocked_on_run_id IS NOT NULL;

CREATE INDEX idx_run_document_versions_document
    ON run_document_versions(document_version_id);

CREATE INDEX idx_run_steps_run_stage_status
    ON run_steps(run_id, stage, status);

CREATE INDEX idx_run_steps_awaiting_upstream
    ON run_steps(run_id, stage)
    WHERE status = 'AWAITING_UPSTREAM';

CREATE INDEX idx_facts_origin_run
    ON facts(origin_run_id);

CREATE INDEX idx_run_facts_fact
    ON run_facts(fact_id);

CREATE INDEX idx_findings_origin_run
    ON findings(origin_run_id);

CREATE INDEX idx_findings_pending
    ON findings(origin_run_id)
    WHERE status = 'PENDING';

CREATE INDEX idx_run_findings_finding
    ON run_findings(finding_id);

CREATE INDEX idx_review_decisions_finding
    ON review_decisions(finding_id);

CREATE INDEX idx_outputs_run
    ON outputs(run_id);

CREATE INDEX idx_audit_events_run_created
    ON audit_events(run_id, created_at);

CREATE INDEX idx_run_metrics_run_stage
    ON run_metrics(run_id, stage, attempt);

-- ---------------------------------------------------------------------------
-- HNSW VECTOR INDEX
--
-- Assumes cosine similarity/distance for retrieval.
-- Exact parameters can be benchmarked later.
-- ---------------------------------------------------------------------------

CREATE INDEX idx_document_chunks_embedding_hnsw
    ON document_chunks
    USING hnsw (embedding vector_cosine_ops);

COMMIT;