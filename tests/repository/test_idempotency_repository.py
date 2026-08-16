import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg.errors import LockNotAvailable, UniqueViolation

from db.pool import pool
from repositories.idempotency_repository import (
    IdempotencyFailureKind,
    IdempotencyRepository,
    IdempotencyStatus,
    InProgressClaimState,
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
    assert record.claim_generation == 1
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
            record.claim_generation,
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
            record.claim_generation,
        )
        connection.commit()

    assert failed is not None
    assert failed.status is IdempotencyStatus.FAILED
    assert failed.result_reference == "preserved-result"
    assert failed.completed_at is None


def test_workflow_fenced_reopen_requires_exact_retryable_failed_generation(scope: str) -> None:
    original = claim(scope)
    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        failed = repository.mark_failed(
            scope, original.key, original.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert failed is not None
        reopened = repository.reopen_failed(
            scope, original.key, original.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert reopened is not None
        assert reopened.claim_generation == original.claim_generation + 1
        assert reopened.failure_kind is None
        assert repository.reopen_failed(
            scope, original.key, original.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        ) is None
        connection.commit()


def test_two_retry_workers_have_exactly_one_failed_generation_reopen_winner(scope: str) -> None:
    original = claim(scope)
    with pool.connection() as connection:
        failed = IdempotencyRepository(connection).mark_failed(
            scope,
            original.key,
            original.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert failed is not None
        connection.commit()
    barrier = threading.Barrier(2)

    def reopen() -> int | None:
        with pool.connection() as connection:
            barrier.wait(timeout=5)
            reopened = IdempotencyRepository(connection).reopen_failed(
                scope,
                original.key,
                original.claim_generation,
                IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
            )
            connection.commit()
            return reopened.claim_generation if reopened is not None else None

    with ThreadPoolExecutor(max_workers=2) as workers:
        generations = list(workers.map(lambda _: reopen(), range(2)))
    assert generations.count(original.claim_generation + 1) == 1
    assert generations.count(None) == 1
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, original.key)
    assert current is not None
    assert current.status is IdempotencyStatus.IN_PROGRESS
    assert current.claim_generation == original.claim_generation + 1
    assert current.failure_kind is None


def test_get_returns_none_for_unknown_operation(scope: str) -> None:
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(scope, "unknown")

    assert record is None


def test_current_claim_operations_return_none_for_missing_record(scope: str) -> None:
    generation = claim(scope).claim_generation

    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)

        assert repository.reclaim_stale(scope, "unknown", generation, timedelta(minutes=5)) is None
        assert repository.mark_completed(scope, "unknown", generation, "result") is None
        assert repository.mark_failed(scope, "unknown", generation) is None


def test_reclaim_fences_the_previous_claim_token(scope: str) -> None:
    original = claim(scope)
    assert original.claimed_at is not None

    with pool.connection() as stale_connection, pool.connection() as reclaim_connection:
        stale_repository = IdempotencyRepository(stale_connection)
        reclaim_repository = IdempotencyRepository(reclaim_connection)
        reclaim_connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, original.key),
        )
        reclaimed = reclaim_repository.reclaim_stale(
            scope, original.key, original.claim_generation, timedelta(minutes=5),
        )
        reclaim_connection.commit()

        assert reclaimed is not None
        assert reclaimed.status is IdempotencyStatus.IN_PROGRESS
        assert reclaimed.claimed_at is not None
        assert reclaimed.claimed_at != original.claimed_at

        stale_reclaim = stale_repository.reclaim_stale(
            original.scope,
            original.key,
            original.claim_generation,
            timedelta(minutes=5),
        )
        stale_completion = stale_repository.mark_completed(
            original.scope,
            original.key,
            original.claim_generation,
            "stale-result",
        )
        stale_connection.commit()

        current_completion = reclaim_repository.mark_completed(
            reclaimed.scope,
            reclaimed.key,
            reclaimed.claim_generation,
            "current-result",
        )
        reclaim_connection.commit()

    assert stale_reclaim is None
    assert stale_completion is None
    assert current_completion is not None
    assert current_completion.status is IdempotencyStatus.COMPLETED
    assert current_completion.result_reference == "current-result"


def test_classification_uses_database_time_and_observed_generation(scope: str) -> None:
    record = claim(scope)
    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        assert repository.classify_in_progress(
            scope, record.key, record.claim_generation, timedelta(minutes=5),
        ) is InProgressClaimState.MATCHING_FRESH
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, record.key),
        )
        assert repository.classify_in_progress(
            scope, record.key, record.claim_generation, timedelta(minutes=5),
        ) is InProgressClaimState.MATCHING_STALE
        connection.commit()


def test_classification_reports_changed_owner_or_terminal_state(scope: str) -> None:
    record = claim(scope)
    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        assert repository.mark_completed(scope, record.key, record.claim_generation, "done")
        assert repository.classify_in_progress(
            scope, record.key, record.claim_generation, timedelta(minutes=5),
        ) is InProgressClaimState.CHANGED_OR_MISSING
        connection.rollback()

        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, record.key),
        )
        replacement = repository.reclaim_stale(
            scope, record.key, record.claim_generation, timedelta(minutes=5),
        )
        assert replacement is not None
        assert replacement.claim_generation == record.claim_generation + 1
        assert repository.classify_in_progress(
            scope, record.key, record.claim_generation, timedelta(minutes=5),
        ) is InProgressClaimState.CHANGED_OR_MISSING
        connection.commit()


def test_only_one_stale_reclaimer_wins_and_failed_owner_is_fenced(scope: str) -> None:
    original = claim(scope)
    with pool.connection() as first, pool.connection() as second:
        first_repository = IdempotencyRepository(first)
        second_repository = IdempotencyRepository(second)
        first.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, original.key),
        )
        first.commit()
        winner = first_repository.reclaim_stale(
            scope, original.key, original.claim_generation, timedelta(minutes=5),
        )
        first.commit()
        loser = second_repository.reclaim_stale(
            scope, original.key, original.claim_generation, timedelta(minutes=5),
        )
        assert winner is not None
        assert loser is None
        assert second_repository.mark_failed(scope, original.key, original.claim_generation) is None
        assert second_repository.mark_failed(scope, winner.key, winner.claim_generation) is not None
        second.commit()


def test_two_concurrent_stale_reclaimers_have_one_generation_winner(scope: str) -> None:
    original = claim(scope)
    with pool.connection() as connection:
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, original.key),
        )
        connection.commit()
    barrier = threading.Barrier(2)

    def reclaim() -> int | None:
        with pool.connection() as connection:
            barrier.wait(timeout=5)
            winner = IdempotencyRepository(connection).reclaim_stale(
                scope, original.key, original.claim_generation, timedelta(minutes=5),
            )
            connection.commit()
            return winner.claim_generation if winner is not None else None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: reclaim(), range(2)))
    assert results.count(original.claim_generation + 1) == 1
    assert results.count(None) == 1


def test_completion_and_stale_reclaim_race_have_one_authoritative_winner(scope: str) -> None:
    original = claim(scope)
    with pool.connection() as connection:
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, original.key),
        )
        connection.commit()
    barrier = threading.Barrier(2)

    def complete() -> bool:
        with pool.connection() as connection:
            barrier.wait(timeout=5)
            completed = IdempotencyRepository(connection).mark_completed(
                scope, original.key, original.claim_generation, "winner",
            )
            connection.commit()
            return completed is not None

    def reclaim() -> int | None:
        with pool.connection() as connection:
            barrier.wait(timeout=5)
            reclaimed = IdempotencyRepository(connection).reclaim_stale(
                scope, original.key, original.claim_generation, timedelta(minutes=5),
            )
            connection.commit()
            return reclaimed.claim_generation if reclaimed is not None else None

    with ThreadPoolExecutor(max_workers=2) as workers:
        completion = workers.submit(complete)
        reclaim_result = workers.submit(reclaim)
        completion_won = completion.result()
        reclaimed_generation = reclaim_result.result()
    assert completion_won ^ (reclaimed_generation == original.claim_generation + 1)
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, original.key)
    assert current is not None
    if completion_won:
        assert current.status is IdempotencyStatus.COMPLETED
        assert current.claim_generation == original.claim_generation
    else:
        assert current.status is IdempotencyStatus.IN_PROGRESS
        assert current.claim_generation == original.claim_generation + 1


def test_successful_stale_reclaim_rolls_back_to_original_owner(scope: str) -> None:
    original = claim(scope)
    with pool.connection() as connection:
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, original.key),
        )
        connection.commit()
    with pool.connection() as connection:
        before = IdempotencyRepository(connection).get(scope, original.key)
        assert before is not None and before.claimed_at is not None
    with pool.connection() as connection:
        replacement = IdempotencyRepository(connection).reclaim_stale(
            scope, original.key, original.claim_generation, timedelta(minutes=5),
        )
        assert replacement is not None
        assert replacement.claim_generation == original.claim_generation + 1
        connection.rollback()
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get(scope, original.key)
    assert durable is not None
    assert durable.status is IdempotencyStatus.IN_PROGRESS
    assert durable.claim_generation == original.claim_generation
    assert durable.claimed_at == before.claimed_at


def test_current_claim_operations_return_none_for_stale_or_non_progress_record(
    scope: str,
) -> None:
    record = claim(scope)
    assert record.claimed_at is not None

    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        assert repository.mark_failed(record.scope, record.key, record.claim_generation) is not None
        connection.commit()

        assert repository.reclaim_stale(record.scope, record.key, record.claim_generation, timedelta(minutes=5)) is None
        assert repository.mark_completed(record.scope, record.key, record.claim_generation, "result") is None
        assert repository.mark_failed(record.scope, record.key, record.claim_generation) is None


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
