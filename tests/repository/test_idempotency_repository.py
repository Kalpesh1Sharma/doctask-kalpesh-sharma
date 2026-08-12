from uuid import uuid4

import pytest
from psycopg.errors import LockNotAvailable, UniqueViolation

from db.pool import pool
from repositories.idempotency_repository import (
    IdempotencyRepository,
    IdempotencyStatus,
)


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def scope() -> str:
    created_scope = f"idempotency-repository-test-{uuid4()}"

    try:
        yield created_scope
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE scope = %s",
                (created_scope,),
            )
            connection.commit()


def claim(scope: str, key: str = "operation"):
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        connection.commit()

    assert record is not None
    return record


def test_claim_and_get_persist_initial_record(scope: str) -> None:
    record = claim(scope)

    with pool.connection() as connection:
        retrieved = IdempotencyRepository(connection).get(scope, record.key)

    assert record.record_id
    assert record.scope == scope
    assert record.key == "operation"
    assert record.status is IdempotencyStatus.IN_PROGRESS
    assert record.result_reference is None
    assert record.claimed_at is not None
    assert record.created_at is not None
    assert record.completed_at is None
    assert retrieved == record


def test_claim_returns_none_for_existing_operation(scope: str) -> None:
    first = claim(scope)

    with pool.connection() as connection:
        duplicate = IdempotencyRepository(connection).claim(scope, first.key)
        connection.commit()

    assert duplicate is None


def test_database_rejects_duplicate_scope_and_key(scope: str) -> None:
    record = claim(scope)

    with pool.connection() as connection:
        with pytest.raises(UniqueViolation):
            connection.execute(
                """
                INSERT INTO idempotency_records (scope, key, status, claimed_at)
                VALUES (%s, %s, 'IN_PROGRESS'::idempotency_status, clock_timestamp())
                """,
                (record.scope, record.key),
            )

        connection.rollback()


def test_mark_completed_sets_result_reference_and_completion_time(scope: str) -> None:
    record = claim(scope)
    assert record.claimed_at is not None

    with pool.connection() as connection:
        completed = IdempotencyRepository(connection).mark_completed(
            record.scope,
            record.key,
            record.claimed_at,
            "result-123",
        )
        connection.commit()

    assert completed is not None
    assert completed.status is IdempotencyStatus.COMPLETED
    assert completed.result_reference == "result-123"
    assert completed.claimed_at == record.claimed_at
    assert completed.completed_at is not None


def test_mark_failed_preserves_result_and_completion_fields(scope: str) -> None:
    record = claim(scope)
    assert record.claimed_at is not None

    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE idempotency_records
            SET result_reference = %s
            WHERE scope = %s
              AND key = %s
            """,
            ("preserved-result", record.scope, record.key),
        )
        failed = IdempotencyRepository(connection).mark_failed(
            record.scope,
            record.key,
            record.claimed_at,
        )
        connection.commit()

    assert failed is not None
    assert failed.status is IdempotencyStatus.FAILED
    assert failed.result_reference == "preserved-result"
    assert failed.completed_at is None


def test_get_returns_none_for_unknown_operation(scope: str) -> None:
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(scope, "unknown")

    assert record is None


def test_current_claim_operations_return_none_for_missing_record(scope: str) -> None:
    token = claim(scope).claimed_at
    assert token is not None

    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)

        assert repository.reclaim(scope, "unknown", token) is None
        assert repository.mark_completed(scope, "unknown", token, "result") is None
        assert repository.mark_failed(scope, "unknown", token) is None


def test_reclaim_fences_the_previous_claim_token(scope: str) -> None:
    original = claim(scope)
    assert original.claimed_at is not None

    with pool.connection() as stale_connection, pool.connection() as reclaim_connection:
        stale_repository = IdempotencyRepository(stale_connection)
        reclaim_repository = IdempotencyRepository(reclaim_connection)
        reclaimed = reclaim_repository.reclaim(scope, original.key, original.claimed_at)
        reclaim_connection.commit()

        assert reclaimed is not None
        assert reclaimed.status is IdempotencyStatus.IN_PROGRESS
        assert reclaimed.claimed_at is not None
        assert reclaimed.claimed_at != original.claimed_at

        stale_reclaim = stale_repository.reclaim(
            original.scope,
            original.key,
            original.claimed_at,
        )
        stale_completion = stale_repository.mark_completed(
            original.scope,
            original.key,
            original.claimed_at,
            "stale-result",
        )
        stale_connection.commit()

        current_completion = reclaim_repository.mark_completed(
            reclaimed.scope,
            reclaimed.key,
            reclaimed.claimed_at,
            "current-result",
        )
        reclaim_connection.commit()

    assert stale_reclaim is None
    assert stale_completion is None
    assert current_completion is not None
    assert current_completion.status is IdempotencyStatus.COMPLETED
    assert current_completion.result_reference == "current-result"


def test_current_claim_operations_return_none_for_stale_or_non_progress_record(
    scope: str,
) -> None:
    record = claim(scope)
    assert record.claimed_at is not None

    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        assert repository.mark_failed(record.scope, record.key, record.claimed_at) is not None
        connection.commit()

        assert repository.reclaim(record.scope, record.key, record.claimed_at) is None
        assert repository.mark_completed(record.scope, record.key, record.claimed_at, "result") is None
        assert repository.mark_failed(record.scope, record.key, record.claimed_at) is None


def test_caller_rollback_removes_uncommitted_claim(scope: str) -> None:
    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        record = repository.claim(scope, "rollback")
        connection.rollback()

    assert record is not None

    with pool.connection() as connection:
        persisted = IdempotencyRepository(connection).get(scope, "rollback")

    assert persisted is None

    with pool.connection() as connection:
        reclaimed = IdempotencyRepository(connection).claim(scope, "rollback")
        connection.commit()

    assert reclaimed is not None


def test_concurrent_claim_waits_for_unique_key_and_returns_none(scope: str) -> None:
    with pool.connection() as first_connection, pool.connection() as second_connection:
        first_repository = IdempotencyRepository(first_connection)
        second_repository = IdempotencyRepository(second_connection)

        first_claim = first_repository.claim(scope, "contended")
        assert first_claim is not None
        second_connection.execute("SET LOCAL lock_timeout = '100ms'")

        with pytest.raises(LockNotAvailable):
            second_repository.claim(scope, "contended")

        second_connection.rollback()
        first_connection.commit()

        assert second_repository.claim(scope, "contended") is None
        second_connection.commit()
