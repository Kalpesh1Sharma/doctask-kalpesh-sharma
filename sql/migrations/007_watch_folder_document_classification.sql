-- Phase 6C-1: grounded semantic document-role classification for watched ingestion.
-- Classification is immutable historical metadata on a READY DocumentVersion.

BEGIN;

CREATE TYPE document_role AS ENUM (
    'LOAN_ESTIMATE',
    'CLOSING_DISCLOSURE',
    'PROMISSORY_NOTE',
    'SETTLEMENT_STATEMENT',
    'UNDERWRITING_APPROVAL',
    'UNKNOWN'
);

CREATE TABLE document_classifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_version_id UUID NOT NULL UNIQUE
        REFERENCES document_versions(id) ON DELETE RESTRICT,
    role document_role NOT NULL,
    confidence NUMERIC(6,5) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    explanation TEXT NOT NULL CHECK (btrim(explanation) <> ''),
    input_fingerprint TEXT NOT NULL
        CHECK (input_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    provider_name TEXT NOT NULL CHECK (btrim(provider_name) <> ''),
    model_name TEXT,
    input_tokens BIGINT CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens BIGINT CHECK (output_tokens IS NULL OR output_tokens >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE document_classification_evidence (
    classification_id UUID NOT NULL
        REFERENCES document_classifications(id) ON DELETE RESTRICT,
    chunk_id UUID NOT NULL
        REFERENCES document_chunks(id) ON DELETE RESTRICT,
    evidence_order INTEGER NOT NULL CHECK (evidence_order >= 0),
    PRIMARY KEY (classification_id, chunk_id),
    UNIQUE (classification_id, evidence_order)
);

CREATE OR REPLACE FUNCTION validate_document_classification_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    version_status document_version_status;
BEGIN
    SELECT processing_status
    INTO version_status
    FROM document_versions
    WHERE id = NEW.document_version_id;

    IF version_status IS NULL THEN
        RAISE EXCEPTION 'document classification references missing DocumentVersion';
    END IF;

    IF version_status <> 'READY'::document_version_status THEN
        RAISE EXCEPTION 'document classification requires a READY DocumentVersion';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_document_classification_insert
BEFORE INSERT ON document_classifications
FOR EACH ROW EXECUTE FUNCTION validate_document_classification_insert();

CREATE OR REPLACE FUNCTION validate_document_classification_evidence_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    classified_version UUID;
    evidence_version UUID;
BEGIN
    SELECT document_version_id
    INTO classified_version
    FROM document_classifications
    WHERE id = NEW.classification_id;

    SELECT document_version_id
    INTO evidence_version
    FROM document_chunks
    WHERE id = NEW.chunk_id;

    IF classified_version IS NULL OR evidence_version IS NULL THEN
        RAISE EXCEPTION 'document classification evidence references missing durable rows';
    END IF;

    IF classified_version <> evidence_version THEN
        RAISE EXCEPTION 'classification evidence must belong to the classified DocumentVersion';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_document_classification_evidence_insert
BEFORE INSERT ON document_classification_evidence
FOR EACH ROW EXECUTE FUNCTION validate_document_classification_evidence_insert();

CREATE OR REPLACE FUNCTION require_grounding_for_known_document_role()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.role <> 'UNKNOWN'::document_role
       AND NOT EXISTS (
           SELECT 1
           FROM document_classification_evidence
           WHERE classification_id = NEW.id
       ) THEN
        RAISE EXCEPTION 'known document role requires grounded classification evidence';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER trg_require_grounded_document_classification
AFTER INSERT ON document_classifications
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION require_grounding_for_known_document_role();

CREATE OR REPLACE FUNCTION prevent_document_classification_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'document classifications and their evidence are immutable';
END;
$$;

CREATE TRIGGER trg_prevent_document_classification_update_delete
BEFORE UPDATE OR DELETE ON document_classifications
FOR EACH ROW EXECUTE FUNCTION prevent_document_classification_mutation();

CREATE TRIGGER trg_prevent_document_classification_evidence_update_delete
BEFORE UPDATE OR DELETE ON document_classification_evidence
FOR EACH ROW EXECUTE FUNCTION prevent_document_classification_mutation();

COMMIT;
