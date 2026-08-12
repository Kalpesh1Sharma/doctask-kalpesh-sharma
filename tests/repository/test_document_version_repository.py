from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, LockNotAvailable, RaiseException, UniqueViolation

from db.pool import pool
from domain.document_version import DocumentProcessingStatus, DocumentVersion
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository


@dataclass(frozen=True)
class DocumentResources:
    pile_id: str
    document_id: str
    other_document_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> DocumentResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            """
            INSERT INTO piles (name)
            VALUES (%s)
            RETURNING id
            """,
            (f"Document version repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])

        document_repository = DocumentRepository(connection)
        document = document_repository.create(pile_id, "primary")
        other_document = document_repository.create(pile_id, "other")
        connection.commit()

    created_resources = DocumentResources(
        pile_id=pile_id,
        document_id=document.document_id,
        other_document_id=other_document.document_id,
    )

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                UPDATE documents
                SET current_version_id = NULL
                WHERE pile_id = %s::uuid
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


def make_version(
    document_id: str,
    version_number: int,
    *,
    content_hash: str | None = None,
    status: DocumentProcessingStatus = DocumentProcessingStatus.PENDING,
) -> DocumentVersion:
    version_id = str(uuid4())
    return DocumentVersion(
        document_version_id=version_id,
        document_id=document_id,
        version_number=version_number,
        content_hash=content_hash or f"content-{version_id}",
        storage_uri=f"storage://document/{version_id}",
        processing_status=status,
    )


def create_version(
    version: DocumentVersion,
    *,
    mime_type: str | None = None,
    size_bytes: int | None = None,
):
    with pool.connection() as connection:
        document_repository = DocumentRepository(connection)
        assert document_repository.get_for_update(version.document_id) is not None
        record = DocumentVersionRepository(connection).create(version, mime_type, size_bytes)
        connection.commit()

    return record


def mark_ready(document_version_id: str) -> None:
    with pool.connection() as connection:
        repository = DocumentVersionRepository(connection)
        assert (
            repository.update_processing_status(
                document_version_id,
                DocumentProcessingStatus.READY,
            )
            is not None
        )
        connection.commit()


def test_create_persists_version_metadata_and_converts_to_domain(
    resources: DocumentResources,
) -> None:
    version = make_version(resources.document_id, 1)
    record = create_version(version, mime_type="application/pdf", size_bytes=1024)

    assert record.document_version_id == version.document_version_id
    assert record.document_id == version.document_id
    assert record.version_number == 1
    assert record.content_hash == version.content_hash
    assert record.storage_uri == version.storage_uri
    assert record.mime_type == "application/pdf"
    assert record.size_bytes == 1024
    assert record.processing_status is DocumentProcessingStatus.PENDING
    assert record.created_at is not None
    assert record.to_domain() == version


def test_get_by_id_returns_persisted_version_or_none(resources: DocumentResources) -> None:
    version = make_version(resources.document_id, 1)
    created = create_version(version)

    with pool.connection() as connection:
        repository = DocumentVersionRepository(connection)
        retrieved = repository.get_by_id(created.document_version_id)
        missing = repository.get_by_id(str(uuid4()))

    assert retrieved == created
    assert missing is None


def test_list_for_document_orders_versions_by_number(resources: DocumentResources) -> None:
    second = create_version(make_version(resources.document_id, 2))
    first = create_version(make_version(resources.document_id, 1))

    with pool.connection() as connection:
        listed = DocumentVersionRepository(connection).list_for_document(resources.document_id)

    assert listed == [first, second]


def test_next_version_number_is_read_while_caller_holds_document_lock(
    resources: DocumentResources,
) -> None:
    create_version(make_version(resources.document_id, 1))

    with pool.connection() as connection:
        document_repository = DocumentRepository(connection)
        assert document_repository.get_for_update(resources.document_id) is not None
        next_number = DocumentVersionRepository(connection).next_version_number(
            resources.document_id,
        )
        connection.commit()

    assert next_number == 2


def test_database_rejects_non_positive_version_number(resources: DocumentResources) -> None:
    with pool.connection() as connection:
        with pytest.raises(CheckViolation):
            connection.execute(
                """
                INSERT INTO document_versions (
                    document_id,
                    version_number,
                    content_hash,
                    storage_uri
                )
                VALUES (%s::uuid, 0, %s, %s)
                """,
                (resources.document_id, f"content-{uuid4()}", f"storage://{uuid4()}"),
            )

        connection.rollback()


def test_database_rejects_duplicate_version_number(resources: DocumentResources) -> None:
    original = make_version(resources.document_id, 1)
    create_version(original)
    duplicate = make_version(resources.document_id, 1)

    with pool.connection() as connection:
        document_repository = DocumentRepository(connection)
        assert document_repository.get_for_update(resources.document_id) is not None

        with pytest.raises(UniqueViolation):
            DocumentVersionRepository(connection).create(duplicate, None, None)

        connection.rollback()


def test_database_rejects_duplicate_content_hash(resources: DocumentResources) -> None:
    original = make_version(resources.document_id, 1)
    create_version(original)
    duplicate = make_version(
        resources.document_id,
        2,
        content_hash=original.content_hash,
    )

    with pool.connection() as connection:
        document_repository = DocumentRepository(connection)
        assert document_repository.get_for_update(resources.document_id) is not None

        with pytest.raises(UniqueViolation):
            DocumentVersionRepository(connection).create(duplicate, None, None)

        connection.rollback()


def test_update_processing_status_persists_requested_status(
    resources: DocumentResources,
) -> None:
    version = make_version(resources.document_id, 1)
    create_version(version)

    with pool.connection() as connection:
        updated = DocumentVersionRepository(connection).update_processing_status(
            version.document_version_id,
            DocumentProcessingStatus.READY,
        )
        connection.commit()

    assert updated is not None
    assert updated.processing_status is DocumentProcessingStatus.READY


def test_update_processing_status_returns_none_for_unknown_version() -> None:
    with pool.connection() as connection:
        updated = DocumentVersionRepository(connection).update_processing_status(
            str(uuid4()),
            DocumentProcessingStatus.READY,
        )

    assert updated is None


def test_database_enforces_ready_version_immutability(resources: DocumentResources) -> None:
    version = make_version(resources.document_id, 1, status=DocumentProcessingStatus.READY)
    create_version(version)

    with pool.connection() as connection:
        with pytest.raises(RaiseException):
            connection.execute(
                """
                UPDATE document_versions
                SET storage_uri = %s
                WHERE id = %s::uuid
                """,
                ("storage://changed", version.document_version_id),
            )

        connection.rollback()

        with pytest.raises(RaiseException):
            DocumentVersionRepository(connection).update_processing_status(
                version.document_version_id,
                DocumentProcessingStatus.PENDING,
            )

        connection.rollback()


def test_current_version_trigger_enforces_ready_same_document_and_forward_only(
    resources: DocumentResources,
) -> None:
    pending = make_version(resources.document_id, 1)
    first = make_version(resources.document_id, 2, status=DocumentProcessingStatus.READY)
    second = make_version(resources.document_id, 3, status=DocumentProcessingStatus.READY)
    foreign = make_version(resources.other_document_id, 1, status=DocumentProcessingStatus.READY)
    create_version(pending)
    create_version(first)
    create_version(second)
    create_version(foreign)

    with pool.connection() as connection:
        document_repository = DocumentRepository(connection)
        assert document_repository.get_for_update(resources.document_id) is not None

        with pytest.raises(RaiseException):
            document_repository.set_current_version(
                resources.document_id,
                pending.document_version_id,
            )

        connection.rollback()
        assert document_repository.get_for_update(resources.document_id) is not None

        with pytest.raises(RaiseException):
            document_repository.set_current_version(
                resources.document_id,
                foreign.document_version_id,
            )

        connection.rollback()
        assert document_repository.get_for_update(resources.document_id) is not None

        current = document_repository.set_current_version(
            resources.document_id,
            first.document_version_id,
        )
        assert current is not None
        assert current.current_version_id == first.document_version_id
        connection.commit()

        assert document_repository.get_for_update(resources.document_id) is not None
        with pytest.raises(RaiseException):
            document_repository.set_current_version(
                resources.document_id,
                first.document_version_id,
            )

        connection.rollback()
        assert document_repository.get_for_update(resources.document_id) is not None

        advanced = document_repository.set_current_version(
            resources.document_id,
            second.document_version_id,
        )
        assert advanced is not None
        assert advanced.current_version_id == second.document_version_id
        connection.commit()

        assert document_repository.get_for_update(resources.document_id) is not None
        with pytest.raises(RaiseException):
            document_repository.set_current_version(
                resources.document_id,
                first.document_version_id,
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_document_version(
    resources: DocumentResources,
) -> None:
    version = make_version(resources.document_id, 1)

    with pool.connection() as connection:
        document_repository = DocumentRepository(connection)
        assert document_repository.get_for_update(resources.document_id) is not None
        record = DocumentVersionRepository(connection).create(version, None, None)
        connection.rollback()

    with pool.connection() as connection:
        persisted = DocumentVersionRepository(connection).get_by_id(record.document_version_id)

    assert persisted is None


def test_concurrent_version_allocation_uses_caller_held_document_lock(
    resources: DocumentResources,
) -> None:
    first_version = make_version(resources.document_id, 1)
    second_version = make_version(resources.document_id, 2)

    with pool.connection() as first_connection, pool.connection() as second_connection:
        first_document_repository = DocumentRepository(first_connection)
        first_version_repository = DocumentVersionRepository(first_connection)
        second_document_repository = DocumentRepository(second_connection)
        second_version_repository = DocumentVersionRepository(second_connection)

        assert first_document_repository.get_for_update(resources.document_id) is not None
        assert first_version_repository.next_version_number(resources.document_id) == 1
        first_record = first_version_repository.create(first_version, None, None)

        second_connection.execute("SET LOCAL lock_timeout = '100ms'")
        with pytest.raises(LockNotAvailable):
            second_document_repository.get_for_update(resources.document_id)

        second_connection.rollback()
        first_connection.commit()

        assert second_document_repository.get_for_update(resources.document_id) is not None
        assert second_version_repository.next_version_number(resources.document_id) == 2
        second_record = second_version_repository.create(second_version, None, None)
        second_connection.commit()

    assert first_record.version_number == 1
    assert second_record.version_number == 2
