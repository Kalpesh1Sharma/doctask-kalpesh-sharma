from uuid import uuid4

import pytest
from psycopg.errors import LockNotAvailable, UniqueViolation

from db.pool import pool
from repositories.document_repository import DocumentRepository


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def pile_id() -> str:
    with pool.connection() as connection:
        row = connection.execute(
            """
            INSERT INTO piles (name)
            VALUES (%s)
            RETURNING id
            """,
            (f"Document repository test {uuid4()}",),
        ).fetchone()
        connection.commit()

    assert row is not None
    created_pile_id = str(row[0])

    try:
        yield created_pile_id
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM documents WHERE pile_id = %s::uuid",
                (created_pile_id,),
            )
            connection.execute(
                "DELETE FROM piles WHERE id = %s::uuid",
                (created_pile_id,),
            )
            connection.commit()


def create_document(pile_id: str, logical_key: str = "document") -> str:
    with pool.connection() as connection:
        record = DocumentRepository(connection).create(pile_id, logical_key)
        connection.commit()

    return record.document_id


def test_create_persists_document_and_lookup_round_trips(pile_id: str) -> None:
    with pool.connection() as connection:
        repository = DocumentRepository(connection)
        created = repository.create(pile_id, "loan-estimate")
        connection.commit()

    with pool.connection() as connection:
        repository = DocumentRepository(connection)
        by_id = repository.get_by_id(created.document_id)
        by_key = repository.get_by_logical_key(pile_id, created.logical_key)

    assert created.pile_id == pile_id
    assert created.logical_key == "loan-estimate"
    assert created.current_version_id is None
    assert created.created_at is not None
    assert created.updated_at is not None
    assert by_id == created
    assert by_key == created


def test_document_lookups_return_none_when_unknown(pile_id: str) -> None:
    with pool.connection() as connection:
        repository = DocumentRepository(connection)
        by_id = repository.get_by_id(str(uuid4()))
        by_key = repository.get_by_logical_key(pile_id, "unknown")

    assert by_id is None
    assert by_key is None


def test_duplicate_logical_key_in_pile_is_rejected(pile_id: str) -> None:
    create_document(pile_id, "closing-disclosure")

    with pool.connection() as connection:
        with pytest.raises(UniqueViolation):
            DocumentRepository(connection).create(pile_id, "closing-disclosure")

        connection.rollback()


def test_get_for_update_holds_document_lock_until_caller_commits(pile_id: str) -> None:
    document_id = create_document(pile_id)

    with pool.connection() as first_connection, pool.connection() as second_connection:
        first_repository = DocumentRepository(first_connection)
        second_repository = DocumentRepository(second_connection)

        assert first_repository.get_for_update(document_id) is not None
        second_connection.execute("SET LOCAL lock_timeout = '100ms'")

        with pytest.raises(LockNotAvailable):
            second_repository.get_for_update(document_id)

        second_connection.rollback()
        first_connection.commit()

        assert second_repository.get_for_update(document_id) is not None
        second_connection.commit()


def test_caller_rollback_removes_uncommitted_document(pile_id: str) -> None:
    with pool.connection() as connection:
        record = DocumentRepository(connection).create(pile_id, "rollback")
        connection.rollback()

    with pool.connection() as connection:
        persisted = DocumentRepository(connection).get_by_id(record.document_id)

    assert persisted is None
