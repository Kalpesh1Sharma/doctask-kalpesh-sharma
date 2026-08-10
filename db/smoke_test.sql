BEGIN;

-- ============================================================
-- 1. Create temporary test pile
-- ============================================================

CREATE TEMP TABLE smoke_test_ids (
    pile_id UUID,
    document_id UUID,
    version_id UUID
);

WITH new_pile AS (
    INSERT INTO piles (
        name,
        description,
        watch_enabled,
        watch_location
    )
    VALUES (
        'Smoke Test Pile',
        'Temporary database smoke test',
        false,
        NULL
    )
    RETURNING id
),
new_document AS (
    INSERT INTO documents (
        pile_id,
        logical_key
    )
    SELECT
        id,
        'smoke-test-document.pdf'
    FROM new_pile
    RETURNING id, pile_id
),
new_version AS (
    INSERT INTO document_versions (
        document_id,
        version_number,
        content_hash,
        storage_uri,
        mime_type,
        size_bytes,
        processing_status
    )
    SELECT
        id,
        1,
        'smoke-test-hash-v1',
        'smoke://document/v1',
        'application/pdf',
        100,
        'PENDING'
    FROM new_document
    RETURNING id, document_id
)
INSERT INTO smoke_test_ids
SELECT
    p.id,
    d.id,
    v.id
FROM new_pile p
JOIN new_document d ON d.pile_id = p.id
JOIN new_version v ON v.document_id = d.id;


-- ============================================================
-- 2. Verify PENDING → READY succeeds
-- ============================================================

UPDATE document_versions
SET processing_status = 'READY'
WHERE id = (SELECT version_id FROM smoke_test_ids);


-- ============================================================
-- 3. Verify current_version_id can point to the READY version
-- ============================================================

UPDATE documents
SET current_version_id = (
    SELECT version_id
    FROM smoke_test_ids
)
WHERE id = (
    SELECT document_id
    FROM smoke_test_ids
);


-- ============================================================
-- 4. Verify READY version is immutable
-- ============================================================

DO $$
BEGIN
    BEGIN
        UPDATE document_versions
        SET content_hash = 'THIS-MUST-FAIL'
        WHERE id = (SELECT version_id FROM smoke_test_ids);

        RAISE EXCEPTION
            'SMOKE TEST FAILED: READY document version was modified';
    EXCEPTION
        WHEN OTHERS THEN
            IF SQLERRM LIKE 'SMOKE TEST FAILED:%' THEN
                RAISE;
            END IF;

            RAISE NOTICE
                'PASS: READY document version modification was rejected: %',
                SQLERRM;
    END;
END
$$;


-- ============================================================
-- 5. Verify current_version_id points to the correct document
-- ============================================================

DO $$
BEGIN
    BEGIN
        UPDATE documents
        SET current_version_id = gen_random_uuid()
        WHERE id = (
            SELECT document_id
            FROM smoke_test_ids
        );

        RAISE EXCEPTION
            'SMOKE TEST FAILED: invalid current_version_id was accepted';
    EXCEPTION
        WHEN OTHERS THEN
            IF SQLERRM LIKE 'SMOKE TEST FAILED:%' THEN
                RAISE;
            END IF;

            RAISE NOTICE
                'PASS: invalid current_version_id was rejected: %',
                SQLERRM;
    END;
END
$$;


-- ============================================================
-- 6. Show successful test state
-- ============================================================

SELECT
    p.name AS pile_name,
    d.logical_key,
    dv.version_number,
    dv.processing_status,
    (d.current_version_id = dv.id) AS is_current_version
FROM smoke_test_ids s
JOIN piles p
    ON p.id = s.pile_id
JOIN documents d
    ON d.id = s.document_id
JOIN document_versions dv
    ON dv.id = s.version_id;


-- ============================================================
-- IMPORTANT:
-- This is a smoke test only. Do not leave test data behind.
-- ============================================================

ROLLBACK;