from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from db.pool import pool
from domain.document_version import DocumentVersion
from domain.evidence import EvidenceSpan
from repositories.document_chunk_repository import DocumentChunkRepository
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.evidence_span_repository import EvidenceSpanRepository


@dataclass(frozen=True)
class EvidenceResources:
    pile_id: str
    document_version_id: str
    other_document_version_id: str
    chunk_id: str
    other_chunk_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> EvidenceResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            """
            INSERT INTO piles (name)
            VALUES (%s)
            RETURNING id
            """,
            (f"Evidence span repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])

        document_repository = DocumentRepository(connection)
        document = document_repository.create(pile_id, "primary")
        other_document = document_repository.create(pile_id, "other")
        version_repository = DocumentVersionRepository(connection)
        version = make_version(document.document_id, 1)
        other_version = make_version(other_document.document_id, 1)
        assert document_repository.get_for_update(document.document_id) is not None
        version_repository.create(version, None, None)
        assert document_repository.get_for_update(other_document.document_id) is not None
        version_repository.create(other_version, None, None)

        chunk_repository = DocumentChunkRepository(connection)
        chunk = chunk_repository.create(version.document_version_id, 0, "primary", "chunk-primary")
        other_chunk = chunk_repository.create(
            other_version.document_version_id,
            0,
            "other",
            "chunk-other",
        )
        connection.commit()

    created_resources = EvidenceResources(
        pile_id=pile_id,
        document_version_id=version.document_version_id,
        other_document_version_id=other_version.document_version_id,
        chunk_id=chunk.chunk_id,
        other_chunk_id=other_chunk.chunk_id,
    )

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM evidence_spans
                WHERE document_version_id IN (
                    SELECT document_versions.id
                    FROM document_versions
                    JOIN documents ON documents.id = document_versions.document_id
                    WHERE documents.pile_id = %s::uuid
                )
                """,
                (created_resources.pile_id,),
            )
            connection.execute(
                """
                DELETE FROM document_chunks
                WHERE document_version_id IN (
                    SELECT document_versions.id
                    FROM document_versions
                    JOIN documents ON documents.id = document_versions.document_id
                    WHERE documents.pile_id = %s::uuid
                )
                """,
                (created_resources.pile_id,),
            )
            connection.execute(
                """
                DELETE FROM document_versions
                WHERE document_id IN (
                    SELECT id
                    FROM documents
                    WHERE pile_id = %s::uuid
                )
                """,
                (created_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM documents WHERE pile_id = %s::uuid",
                (created_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM piles WHERE id = %s::uuid",
                (created_resources.pile_id,),
            )
            connection.commit()


def make_version(document_id: str, version_number: int) -> DocumentVersion:
    version_id = str(uuid4())
    return DocumentVersion(
        document_version_id=version_id,
        document_id=document_id,
        version_number=version_number,
        content_hash=f"content-{version_id}",
        storage_uri=f"storage://document/{version_id}",
    )


def make_span(
    resources: EvidenceResources,
    *,
    evidence_id: str | None = None,
    document_version_id: str | None = None,
    chunk_id: str | None = None,
) -> EvidenceSpan:
    return EvidenceSpan(
        evidence_id=evidence_id or str(uuid4()),
        document_version_id=document_version_id or resources.document_version_id,
        chunk_id=chunk_id or resources.chunk_id,
        page_number=2,
        start_offset=10,
        end_offset=20,
        source_text="The loan amount is $100,000.",
        content_hash="evidence-hash",
    )


def test_create_get_list_and_to_domain_round_trip(resources: EvidenceResources) -> None:
    with pool.connection() as connection:
        repository = EvidenceSpanRepository(connection)
        first = repository.create(make_span(resources))
        second = repository.create(make_span(resources))
        connection.commit()

    with pool.connection() as connection:
        repository = EvidenceSpanRepository(connection)
        retrieved = repository.get_by_id(first.evidence_id)
        listed = repository.list_for_document_version(resources.document_version_id)
        missing = repository.get_by_id(str(uuid4()))

    assert first.document_version_id == resources.document_version_id
    assert first.chunk_id == resources.chunk_id
    assert first.page_number == 2
    assert first.start_offset == 10
    assert first.end_offset == 20
    assert first.source_text == "The loan amount is $100,000."
    assert first.created_at is not None
    assert first.to_domain() == make_span(resources, evidence_id=first.evidence_id)
    assert retrieved == first
    assert listed == sorted(
        [first, second],
        key=lambda record: (record.created_at, record.evidence_id),
    )
    assert missing is None


def test_database_rejects_invalid_evidence_location_values(resources: EvidenceResources) -> None:
    cases = [
        (0, None, None),
        (None, -1, None),
        (None, None, -1),
        (None, 20, 10),
    ]

    with pool.connection() as connection:
        for page_number, start_offset, end_offset in cases:
            with pytest.raises(CheckViolation):
                connection.execute(
                    """
                    INSERT INTO evidence_spans (
                        document_version_id,
                        chunk_id,
                        page_number,
                        start_offset,
                        end_offset,
                        quoted_text,
                        content_hash
                    )
                    VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, %s)
                    """,
                    (
                        resources.document_version_id,
                        resources.chunk_id,
                        page_number,
                        start_offset,
                        end_offset,
                        "invalid",
                        f"invalid-{uuid4()}",
                    ),
                )

            connection.rollback()


def test_repository_exposes_evidence_foreign_key_backstops(resources: EvidenceResources) -> None:
    with pool.connection() as connection:
        repository = EvidenceSpanRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.create(
                make_span(
                    resources,
                    document_version_id=str(uuid4()),
                ),
            )

        connection.rollback()

        with pytest.raises(ForeignKeyViolation):
            repository.create(
                make_span(
                    resources,
                    chunk_id=str(uuid4()),
                ),
            )

        connection.rollback()

        with pytest.raises(ForeignKeyViolation):
            repository.create(
                make_span(
                    resources,
                    chunk_id=resources.other_chunk_id,
                ),
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_evidence(resources: EvidenceResources) -> None:
    with pool.connection() as connection:
        record = EvidenceSpanRepository(connection).create(make_span(resources))
        connection.rollback()

    with pool.connection() as connection:
        persisted = EvidenceSpanRepository(connection).get_by_id(record.evidence_id)

    assert persisted is None
