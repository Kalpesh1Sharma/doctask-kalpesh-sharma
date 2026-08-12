from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, DataException, ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.document_version import DocumentVersion
from repositories.document_chunk_repository import DocumentChunkRepository
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository


@dataclass(frozen=True)
class ChunkResources:
    pile_id: str
    document_version_id: str
    other_document_version_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> ChunkResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            """
            INSERT INTO piles (name)
            VALUES (%s)
            RETURNING id
            """,
            (f"Document chunk repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])

        document_repository = DocumentRepository(connection)
        document = document_repository.create(pile_id, "primary")
        other_document = document_repository.create(pile_id, "other")
        version_repository = DocumentVersionRepository(connection)
        first_version = make_version(document.document_id, 1)
        second_version = make_version(other_document.document_id, 1)
        assert document_repository.get_for_update(document.document_id) is not None
        version_repository.create(first_version, None, None)
        assert document_repository.get_for_update(other_document.document_id) is not None
        version_repository.create(second_version, None, None)
        connection.commit()

    created_resources = ChunkResources(
        pile_id=pile_id,
        document_version_id=first_version.document_version_id,
        other_document_version_id=second_version.document_version_id,
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


def vector(index: int) -> tuple[float, ...]:
    values = [0.0] * 1536
    values[index] = 1.0
    return tuple(values)


def create_chunk(
    document_version_id: str,
    chunk_index: int = 0,
    *,
    embedding: tuple[float, ...] | None = None,
):
    with pool.connection() as connection:
        record = DocumentChunkRepository(connection).create(
            document_version_id,
            chunk_index,
            f"chunk content {chunk_index}",
            f"chunk-hash-{uuid4()}",
            embedding=embedding,
        )
        connection.commit()

    return record


def test_create_get_and_list_persist_chunk_fields(resources: ChunkResources) -> None:
    second = create_chunk(resources.document_version_id, 2)

    with pool.connection() as connection:
        repository = DocumentChunkRepository(connection)
        first = repository.create(
            resources.document_version_id,
            0,
            "loan amount",
            "chunk-hash-1",
            page_number=2,
            start_offset=10,
            end_offset=20,
            embedding=vector(0),
        )
        connection.commit()

    with pool.connection() as connection:
        repository = DocumentChunkRepository(connection)
        retrieved = repository.get_by_id(first.chunk_id)
        listed = repository.list_for_document_version(resources.document_version_id)
        missing = repository.get_by_id(str(uuid4()))

    assert first.document_version_id == resources.document_version_id
    assert first.chunk_index == 0
    assert first.content == "loan amount"
    assert first.content_hash == "chunk-hash-1"
    assert first.page_number == 2
    assert first.start_offset == 10
    assert first.end_offset == 20
    assert first.embedding == vector(0)
    assert first.created_at is not None
    assert retrieved == first
    assert listed == [first, second]
    assert missing is None


def test_chunk_supports_nullable_metadata(resources: ChunkResources) -> None:
    record = create_chunk(resources.document_version_id)

    assert record.page_number is None
    assert record.start_offset is None
    assert record.end_offset is None
    assert record.embedding is None


def test_database_rejects_invalid_chunk_constraints(resources: ChunkResources) -> None:
    cases = [
        ("chunk_index", -1, None, None, None),
        ("page_number", 0, None, None, None),
        ("offsets", None, -1, None, None),
        ("offsets", None, None, -1, None),
        ("offsets", None, 20, 10, None),
    ]

    with pool.connection() as connection:
        for case, value, start_offset, end_offset, _ in cases:
            chunk_index = value if case == "chunk_index" else 0
            page_number = value if case == "page_number" else None

            with pytest.raises(CheckViolation):
                connection.execute(
                    """
                    INSERT INTO document_chunks (
                        document_version_id,
                        chunk_index,
                        content,
                        content_hash,
                        page_number,
                        start_offset,
                        end_offset
                    )
                    VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        resources.document_version_id,
                        chunk_index,
                        "invalid",
                        f"invalid-{uuid4()}",
                        page_number,
                        start_offset,
                        end_offset,
                    ),
                )

            connection.rollback()


def test_database_rejects_duplicate_chunk_index(resources: ChunkResources) -> None:
    create_chunk(resources.document_version_id)

    with pool.connection() as connection:
        with pytest.raises(UniqueViolation):
            DocumentChunkRepository(connection).create(
                resources.document_version_id,
                0,
                "duplicate",
                f"chunk-hash-{uuid4()}",
            )

        connection.rollback()


def test_database_rejects_unknown_document_version() -> None:
    with pool.connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            DocumentChunkRepository(connection).create(
                str(uuid4()),
                0,
                "unknown",
                "unknown-hash",
            )

        connection.rollback()


def test_database_rejects_wrong_stored_vector_dimension(resources: ChunkResources) -> None:
    with pool.connection() as connection:
        with pytest.raises(DataException):
            DocumentChunkRepository(connection).create(
                resources.document_version_id,
                0,
                "wrong dimension",
                "wrong-dimension-hash",
                embedding=(0.0,) * 1535,
            )

        connection.rollback()


def test_vector_search_orders_scopes_limits_and_excludes_null_embeddings(
    resources: ChunkResources,
) -> None:
    near = create_chunk(resources.document_version_id, 0, embedding=vector(0))
    tie_second = create_chunk(resources.document_version_id, 1, embedding=vector(1))
    tie_first = create_chunk(resources.document_version_id, 2, embedding=vector(1))
    create_chunk(resources.document_version_id, 3)
    other_version = create_chunk(resources.other_document_version_id, 0, embedding=vector(0))

    with pool.connection() as connection:
        repository = DocumentChunkRepository(connection)
        results = repository.search_similar(vector(0), [resources.document_version_id], 10)
        limited = repository.search_similar(vector(0), [resources.document_version_id], 1)
        empty_scope = repository.search_similar(vector(0), [], 10)

    assert [result.chunk.chunk_id for result in results] == [
        near.chunk_id,
        *sorted([tie_first.chunk_id, tie_second.chunk_id]),
    ]
    assert results[0].cosine_distance == 0.0
    assert all(result.chunk.chunk_id != other_version.chunk_id for result in results)
    assert [result.chunk.chunk_id for result in limited] == [near.chunk_id]
    assert empty_scope == []


def test_database_rejects_wrong_query_vector_dimension(resources: ChunkResources) -> None:
    create_chunk(resources.document_version_id, embedding=vector(0))

    with pool.connection() as connection:
        with pytest.raises(DataException):
            DocumentChunkRepository(connection).search_similar(
                (0.0,) * 1535,
                [resources.document_version_id],
                1,
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_chunk(resources: ChunkResources) -> None:
    with pool.connection() as connection:
        record = DocumentChunkRepository(connection).create(
            resources.document_version_id,
            0,
            "rollback",
            "rollback-hash",
        )
        connection.rollback()

    with pool.connection() as connection:
        persisted = DocumentChunkRepository(connection).get_by_id(record.chunk_id)

    assert persisted is None
