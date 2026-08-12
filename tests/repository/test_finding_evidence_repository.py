from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.document_version import DocumentVersion
from domain.evidence import EvidenceSpan
from domain.finding import Finding, FindingType
from repositories.document_chunk_repository import DocumentChunkRepository
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.finding_evidence_repository import FindingEvidenceRepository
from repositories.finding_repository import FindingRepository


@dataclass(frozen=True)
class FindingEvidenceResources:
    pile_id: str
    run_id: str
    first_finding_id: str
    second_finding_id: str
    first_evidence_id: str
    second_evidence_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> FindingEvidenceResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Finding evidence repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        run_id = create_manual_run(connection, pile_id)

        document_repository = DocumentRepository(connection)
        document = document_repository.create(pile_id, "evidence-document")
        assert document_repository.get_for_update(document.document_id) is not None
        version = make_version(document.document_id)
        DocumentVersionRepository(connection).create(version, None, None)
        chunk = DocumentChunkRepository(connection).create(
            version.document_version_id,
            0,
            "loan amount",
            f"chunk-{uuid4()}",
        )
        evidence_repository = EvidenceSpanRepository(connection)
        first_evidence = evidence_repository.create(make_evidence(version.document_version_id, chunk.chunk_id))
        second_evidence = evidence_repository.create(make_evidence(version.document_version_id, chunk.chunk_id))
        first_finding_id = create_finding(connection, run_id)
        second_finding_id = create_finding(connection, run_id)
        connection.commit()

    created_resources = FindingEvidenceResources(
        pile_id,
        run_id,
        first_finding_id,
        second_finding_id,
        first_evidence.evidence_id,
        second_evidence.evidence_id,
    )

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM finding_evidence
                WHERE finding_id IN (
                    SELECT id FROM findings WHERE origin_run_id = %s::uuid
                )
                """,
                (created_resources.run_id,),
            )
            connection.execute(
                "DELETE FROM run_findings WHERE run_id = %s::uuid", (created_resources.run_id,))
            connection.execute(
                "DELETE FROM findings WHERE origin_run_id = %s::uuid",
                (created_resources.run_id,),
            )
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
                    SELECT id FROM documents WHERE pile_id = %s::uuid
                )
                """,
                (created_resources.pile_id,),
            )
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (created_resources.pile_id,))
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (created_resources.run_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created_resources.pile_id,))
            connection.commit()


def create_manual_run(connection, pile_id: str) -> str:
    run_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO runs (id, pile_id, run_type, status)
        VALUES (%s::uuid, %s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
        """,
        (run_id, pile_id),
    )
    return run_id


def make_version(document_id: str) -> DocumentVersion:
    version_id = str(uuid4())
    return DocumentVersion(
        document_version_id=version_id,
        document_id=document_id,
        version_number=1,
        content_hash=f"content-{version_id}",
        storage_uri=f"storage://document/{version_id}",
    )


def make_evidence(document_version_id: str, chunk_id: str) -> EvidenceSpan:
    evidence_id = str(uuid4())
    return EvidenceSpan(
        evidence_id=evidence_id,
        document_version_id=document_version_id,
        chunk_id=chunk_id,
        page_number=1,
        start_offset=0,
        end_offset=10,
        source_text="The loan amount is $100,000.",
        content_hash=f"evidence-{evidence_id}",
    )


def create_finding(connection, run_id: str) -> str:
    finding = Finding(
        finding_id=str(uuid4()),
        origin_run_id=run_id,
        finding_type=FindingType.CONTRADICTION,
        title="Loan amounts conflict",
        severity="HIGH",
    )
    return FindingRepository(connection).create(finding).finding_id


def test_add_get_list_and_many_to_many_evidence_links(resources: FindingEvidenceResources) -> None:
    with pool.connection() as connection:
        repository = FindingEvidenceRepository(connection)
        first = repository.add(resources.first_finding_id, resources.first_evidence_id)
        second = repository.add(resources.first_finding_id, resources.second_evidence_id)
        shared = repository.add(resources.second_finding_id, resources.first_evidence_id)
        connection.commit()

    with pool.connection() as connection:
        repository = FindingEvidenceRepository(connection)
        retrieved = repository.get(resources.first_finding_id, resources.first_evidence_id)
        listed = repository.list_for_finding(resources.first_finding_id)
        missing = repository.get(resources.second_finding_id, resources.second_evidence_id)

    assert retrieved == first
    assert listed == sorted([first, second], key=lambda record: record.evidence_span_id)
    assert shared.evidence_span_id == first.evidence_span_id
    assert missing is None


def test_duplicate_link_is_rejected(resources: FindingEvidenceResources) -> None:
    with pool.connection() as connection:
        repository = FindingEvidenceRepository(connection)
        repository.add(resources.first_finding_id, resources.first_evidence_id)
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.add(resources.first_finding_id, resources.first_evidence_id)

        connection.rollback()


def test_unknown_foreign_keys_are_rejected(resources: FindingEvidenceResources) -> None:
    with pool.connection() as connection:
        repository = FindingEvidenceRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.add(str(uuid4()), resources.first_evidence_id)

        connection.rollback()

        with pytest.raises(ForeignKeyViolation):
            repository.add(resources.first_finding_id, str(uuid4()))

        connection.rollback()


def test_caller_rollback_removes_uncommitted_evidence_link(resources: FindingEvidenceResources) -> None:
    with pool.connection() as connection:
        FindingEvidenceRepository(connection).add(resources.first_finding_id, resources.first_evidence_id)
        connection.rollback()

    with pool.connection() as connection:
        record = FindingEvidenceRepository(connection).get(
            resources.first_finding_id,
            resources.first_evidence_id,
        )

    assert record is None
