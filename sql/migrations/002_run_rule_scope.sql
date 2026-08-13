-- ---------------------------------------------------------------------------
-- IMMUTABLE RUN RULE SCOPE
--
-- This additive migration closes the V2.7 provenance gap for rule evaluation.
-- Snapshot children are staged before their header; deferred FKs require the
-- header before commit, while the header closes the membership set.
-- ---------------------------------------------------------------------------

CREATE TABLE run_rule_scopes (
    run_id UUID PRIMARY KEY
        REFERENCES runs(id) ON DELETE RESTRICT,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE run_rule_set_snapshots (
    run_id UUID NOT NULL,
    source_rule_set_id UUID NOT NULL
        REFERENCES rule_sets(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    description TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, source_rule_set_id),

    CONSTRAINT fk_run_rule_set_snapshot_scope
        FOREIGN KEY (run_id)
        REFERENCES run_rule_scopes(run_id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE run_rule_snapshots (
    run_id UUID NOT NULL,
    source_rule_id UUID NOT NULL
        REFERENCES rules(id) ON DELETE RESTRICT,
    source_rule_set_id UUID NOT NULL,
    rule_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    severity TEXT NOT NULL,
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (run_id, source_rule_id),

    CONSTRAINT fk_run_rule_snapshot_scope
        FOREIGN KEY (run_id)
        REFERENCES run_rule_scopes(run_id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED,

    CONSTRAINT fk_run_rule_snapshot_rule_set
        FOREIGN KEY (run_id, source_rule_set_id)
        REFERENCES run_rule_set_snapshots(run_id, source_rule_set_id)
        ON DELETE CASCADE
        DEFERRABLE INITIALLY DEFERRED
);

-- ---------------------------------------------------------------------------
-- CHILD INSERT VALIDATION / SCOPE CLOSURE
--
-- Child inserts and header insertion lock the same Run row. This ensures a
-- child cannot be committed after another transaction has closed the scope.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_run_rule_set_snapshot_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    run_pile_id UUID;
    source_pile_id UUID;
    source_name TEXT;
    source_description TEXT;
    source_version INTEGER;
BEGIN
    SELECT pile_id INTO run_pile_id
    FROM runs
    WHERE id = NEW.run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'run % does not exist', NEW.run_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM run_rule_scopes
        WHERE run_id = NEW.run_id
    ) THEN
        RAISE EXCEPTION 'run rule scope % is already closed', NEW.run_id;
    END IF;

    SELECT pile_id, name, description, version
    INTO source_pile_id, source_name, source_description, source_version
    FROM rule_sets
    WHERE id = NEW.source_rule_set_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rule set % does not exist', NEW.source_rule_set_id;
    END IF;

    IF source_pile_id <> run_pile_id THEN
        RAISE EXCEPTION
            'rule set % must belong to run % pile',
            NEW.source_rule_set_id,
            NEW.run_id;
    END IF;

    IF NEW.name IS DISTINCT FROM source_name
       OR NEW.description IS DISTINCT FROM source_description
       OR NEW.version IS DISTINCT FROM source_version THEN
        RAISE EXCEPTION
            'run rule-set snapshot must copy source rule-set content exactly';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_run_rule_set_snapshot_insert
BEFORE INSERT ON run_rule_set_snapshots
FOR EACH ROW EXECUTE FUNCTION validate_run_rule_set_snapshot_insert();

CREATE OR REPLACE FUNCTION validate_run_rule_snapshot_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    run_pile_id UUID;
    source_pile_id UUID;
    source_rule_set_id UUID;
    source_rule_key TEXT;
    source_title TEXT;
    source_description TEXT;
    source_severity TEXT;
    source_definition JSONB;
BEGIN
    SELECT pile_id INTO run_pile_id
    FROM runs
    WHERE id = NEW.run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'run % does not exist', NEW.run_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM run_rule_scopes
        WHERE run_id = NEW.run_id
    ) THEN
        RAISE EXCEPTION 'run rule scope % is already closed', NEW.run_id;
    END IF;

    SELECT rule_set_id, rule_key, title, description, severity, definition
    INTO
        source_rule_set_id,
        source_rule_key,
        source_title,
        source_description,
        source_severity,
        source_definition
    FROM rules
    WHERE id = NEW.source_rule_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rule % does not exist', NEW.source_rule_id;
    END IF;

    SELECT pile_id INTO source_pile_id
    FROM rule_sets
    WHERE id = source_rule_set_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'rule set % does not exist', source_rule_set_id;
    END IF;

    IF source_rule_set_id <> NEW.source_rule_set_id THEN
        RAISE EXCEPTION 'rule % does not belong to rule set %',
            NEW.source_rule_id,
            NEW.source_rule_set_id;
    END IF;

    IF source_pile_id <> run_pile_id THEN
        RAISE EXCEPTION 'rule % must belong to run % pile', NEW.source_rule_id, NEW.run_id;
    END IF;

    IF NEW.rule_key IS DISTINCT FROM source_rule_key
       OR NEW.title IS DISTINCT FROM source_title
       OR NEW.description IS DISTINCT FROM source_description
       OR NEW.severity IS DISTINCT FROM source_severity
       OR NEW.definition IS DISTINCT FROM source_definition THEN
        RAISE EXCEPTION 'run rule snapshot must copy source rule content exactly';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_run_rule_snapshot_insert
BEFORE INSERT ON run_rule_snapshots
FOR EACH ROW EXECUTE FUNCTION validate_run_rule_snapshot_insert();

CREATE OR REPLACE FUNCTION close_run_rule_scope()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1
    FROM runs
    WHERE id = NEW.run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'run % does not exist', NEW.run_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_close_run_rule_scope
BEFORE INSERT ON run_rule_scopes
FOR EACH ROW EXECUTE FUNCTION close_run_rule_scope();

-- ---------------------------------------------------------------------------
-- SNAPSHOT IMMUTABILITY
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION reject_run_rule_scope_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'run rule scope snapshots are immutable';
END;
$$;

CREATE TRIGGER trg_run_rule_scope_immutable
BEFORE UPDATE ON run_rule_scopes
FOR EACH ROW EXECUTE FUNCTION reject_run_rule_scope_update();

CREATE TRIGGER trg_run_rule_set_snapshot_immutable
BEFORE UPDATE ON run_rule_set_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_run_rule_scope_update();

CREATE TRIGGER trg_run_rule_snapshot_immutable
BEFORE UPDATE ON run_rule_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_run_rule_scope_update();

CREATE OR REPLACE FUNCTION reject_closed_run_rule_scope_child_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1
    FROM runs
    WHERE id = OLD.run_id
    FOR UPDATE;

    IF EXISTS (
        SELECT 1
        FROM run_rule_scopes
        WHERE run_id = OLD.run_id
    ) THEN
        RAISE EXCEPTION 'closed run rule scope snapshots cannot be deleted';
    END IF;

    RETURN OLD;
END;
$$;

CREATE TRIGGER trg_reject_closed_run_rule_set_snapshot_delete
BEFORE DELETE ON run_rule_set_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_closed_run_rule_scope_child_delete();

CREATE TRIGGER trg_reject_closed_run_rule_snapshot_delete
BEFORE DELETE ON run_rule_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_closed_run_rule_scope_child_delete();

CREATE OR REPLACE FUNCTION reject_run_rule_scope_delete()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'closed run rule scopes cannot be deleted';
END;
$$;

CREATE TRIGGER trg_reject_run_rule_scope_delete
BEFORE DELETE ON run_rule_scopes
FOR EACH ROW EXECUTE FUNCTION reject_run_rule_scope_delete();

-- ---------------------------------------------------------------------------
-- FINDING ORIGIN-SNAPSHOT BACKSTOP
--
-- Existing CHECK/FK behavior stays visible for null or unknown rule IDs.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION validate_finding_origin_rule_snapshot()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.finding_type <> 'RULE_VIOLATION'::finding_type
       OR NEW.rule_id IS NULL THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM rules WHERE id = NEW.rule_id) THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM runs WHERE id = NEW.origin_run_id) THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM run_rule_scopes
        JOIN run_rule_snapshots
          ON run_rule_snapshots.run_id = run_rule_scopes.run_id
        WHERE run_rule_scopes.run_id = NEW.origin_run_id
          AND run_rule_snapshots.source_rule_id = NEW.rule_id
    ) THEN
        RAISE EXCEPTION
            'rule violation finding requires an origin-run rule snapshot for rule %',
            NEW.rule_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_finding_origin_rule_snapshot
BEFORE INSERT ON findings
FOR EACH ROW EXECUTE FUNCTION validate_finding_origin_rule_snapshot();

CREATE INDEX idx_run_rule_set_snapshots_run
    ON run_rule_set_snapshots(run_id, source_rule_set_id);

CREATE INDEX idx_run_rule_snapshots_run
    ON run_rule_snapshots(run_id, source_rule_id);
