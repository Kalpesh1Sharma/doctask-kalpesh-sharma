from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.document_version import DocumentProcessingStatus
from repositories.run_document_version_repository import RunDocumentVersionRepository


@dataclass(frozen=True)
class RepositoryResources:
    pile_id: str
    document_id: str
    document_version_ids: tuple[str, str, str]


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> RepositoryResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            """
            INSERT INTO piles (name)
            VALUES (%s)
            RETURNING id
            """,
            (f"Run document version repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])

        document_row = connection.execute(
            """
            INSERT INTO documents (pile_id, logical_key)
            VALUES (%s::uuid, %s)
            RETURNING id
            """,
            (pile_id, f"document-{uuid4()}"),
        ).fetchone()
        assert document_row is not None
        document_id = str(document_row[0])

        version_ids: list[str] = []
        for version_number in range(1, 4):
            version_row = connection.execute(
                """
                INSERT INTO document_versions (
                    document_id,
                    version_number,
                    content_hash,
                    storage_uri
                )
                VALUES (%s::uuid, %s, %s, %s)
                RETURNING id
                """,
                (
                    document_id,
                    version_number,
                    f"content-{uuid4()}",
                    f"storage://document/{uuid4()}",
                ),
            ).fetchone()
            assert version_row is not None
            version_ids.append(str(version_row[0]))

        connection.commit()

    repository_resources = RepositoryResources(
        pile_id=pile_id,
        document_id=document_id,
        document_version_ids=(version_ids[0], version_ids[1], version_ids[2]),
    )

    try:
        yield repository_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM run_document_versions
                WHERE run_id IN (
                    SELECT id
                    FROM runs
                    WHERE pile_id = %s::uuid
                )
                """,
                (repository_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM runs WHERE pile_id = %s::uuid",
                (repository_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM document_versions WHERE document_id = %s::uuid",
                (repository_resources.document_id,),
            )
            connection.execute(
                "DELETE FROM documents WHERE id = %s::uuid",
                (repository_resources.document_id,),
            )
            connection.execute(
                "DELETE FROM piles WHERE id = %s::uuid",
                (repository_resources.pile_id,),
            )
            connection.commit()


def create_manual_run(pile_id: str) -> str:
    run_id = str(uuid4())

    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO runs (id, pile_id, run_type, status)
            VALUES (%s::uuid, %s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
            """,
            (run_id, pile_id),
        )
        connection.commit()

    return run_id


def test_add_get_and_list_membership(resources: RepositoryResources) -> None:
    run_id = create_manual_run(resources.pile_id)
    document_version_id = resources.document_version_ids[0]

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)
        added = repository.add(run_id, document_version_id)
        connection.commit()

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)
        retrieved = repository.get(run_id, document_version_id)
        listed = repository.list_for_run(run_id)

    assert added.run_id == run_id
    assert added.document_version_id == document_version_id
    assert added.created_at is not None
    assert retrieved == added
    assert listed == [added]


def test_list_for_run_returns_multiple_versions_in_stable_order(
    resources: RepositoryResources,
) -> None:
    run_id = create_manual_run(resources.pile_id)

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)
        added = [
            repository.add(run_id, document_version_id)
            for document_version_id in resources.document_version_ids
        ]
        connection.commit()

    with pool.connection() as connection:
        listed = RunDocumentVersionRepository(connection).list_for_run(run_id)

    assert listed == sorted(
        added,
        key=lambda record: (record.created_at, record.document_version_id),
    )


def test_same_document_version_can_belong_to_different_runs(
    resources: RepositoryResources,
) -> None:
    first_run_id = create_manual_run(resources.pile_id)
    second_run_id = create_manual_run(resources.pile_id)
    document_version_id = resources.document_version_ids[0]

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)
        first_record = repository.add(first_run_id, document_version_id)
        second_record = repository.add(second_run_id, document_version_id)
        connection.commit()

    assert first_record.document_version_id == document_version_id
    assert second_record.document_version_id == document_version_id
    assert first_record.run_id != second_record.run_id


def test_get_returns_none_for_unknown_membership(resources: RepositoryResources) -> None:
    run_id = create_manual_run(resources.pile_id)

    with pool.connection() as connection:
        record = RunDocumentVersionRepository(connection).get(
            run_id,
            resources.document_version_ids[0],
        )

    assert record is None


def test_duplicate_membership_is_rejected(resources: RepositoryResources) -> None:
    run_id = create_manual_run(resources.pile_id)
    document_version_id = resources.document_version_ids[0]

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)
        repository.add(run_id, document_version_id)
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.add(run_id, document_version_id)

        connection.rollback()


def test_unknown_run_is_rejected_by_foreign_key(resources: RepositoryResources) -> None:
    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.add(str(uuid4()), resources.document_version_ids[0])

        connection.rollback()


def test_unknown_document_version_is_rejected_by_foreign_key(
    resources: RepositoryResources,
) -> None:
    run_id = create_manual_run(resources.pile_id)

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.add(run_id, str(uuid4()))

        connection.rollback()


def test_caller_rollback_removes_uncommitted_membership(
    resources: RepositoryResources,
) -> None:
    run_id = create_manual_run(resources.pile_id)
    document_version_id = resources.document_version_ids[0]

    with pool.connection() as connection:
        repository = RunDocumentVersionRepository(connection)
        repository.add(run_id, document_version_id)
        connection.rollback()

    with pool.connection() as connection:
        record = RunDocumentVersionRepository(connection).get(run_id, document_version_id)

    assert record is None


def test_list_scope_for_run_joins_document_identity_and_readiness(
    resources: RepositoryResources,
) -> None:
    run_id = create_manual_run(resources.pile_id)
    with pool.connection() as connection:
        connection.execute(
            "UPDATE document_versions SET processing_status = 'READY' WHERE id = %s::uuid",
            (resources.document_version_ids[1],),
        )
        repository = RunDocumentVersionRepository(connection)
        repository.add(run_id, resources.document_version_ids[2])
        repository.add(run_id, resources.document_version_ids[1])
        connection.commit()

    with pool.connection() as connection:
        records = RunDocumentVersionRepository(connection).list_scope_for_run(run_id)
        missing = RunDocumentVersionRepository(connection).list_scope_for_run(str(uuid4()))

    assert [record.document_version_id for record in records] == list(resources.document_version_ids[1:])
    assert all(record.run_id == run_id for record in records)
    assert all(record.document_id == resources.document_id for record in records)
    assert all(record.document_pile_id == resources.pile_id for record in records)
    assert records[0].version_number == 2
    assert records[0].processing_status is DocumentProcessingStatus.READY
    assert records[1].processing_status is DocumentProcessingStatus.PENDING
    assert missing == []
