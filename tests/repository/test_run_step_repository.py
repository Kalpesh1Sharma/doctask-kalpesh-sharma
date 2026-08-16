from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException, UniqueViolation

from db.pool import pool
from domain.run import RunStage, RunStepStatus
from repositories.idempotency_repository import IdempotencyFailureKind, IdempotencyRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_step_repository import RunStepRepository


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
            (f"Run step repository test {uuid4()}",),
        ).fetchone()
        connection.commit()

    assert row is not None
    created_pile_id = str(row[0])

    try:
        yield created_pile_id
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM run_metrics
                WHERE run_id IN (
                    SELECT id FROM runs WHERE pile_id = %s::uuid
                )
                """,
                (created_pile_id,),
            )
            connection.execute(
                """
                DELETE FROM run_steps
                WHERE run_id IN (
                    SELECT id
                    FROM runs
                    WHERE pile_id = %s::uuid
                )
                """,
                (created_pile_id,),
            )
            connection.execute(
                "DELETE FROM runs WHERE pile_id = %s::uuid",
                (created_pile_id,),
            )
            connection.execute(
                "DELETE FROM piles WHERE id = %s::uuid",
                (created_pile_id,),
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


def create_step(
    run_id: str,
    stage: RunStage = RunStage.INGEST,
    attempt: int = 1,
) -> None:
    with pool.connection() as connection:
        RunStepRepository(connection).create(run_id, stage, attempt)
        connection.commit()


def test_create_persists_step_and_returns_record(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        record = RunStepRepository(connection).create(run_id, RunStage.EXAMINE, 1)
        connection.commit()

    assert record.step_id
    assert record.run_id == run_id
    assert record.stage is RunStage.EXAMINE
    assert record.status is RunStepStatus.PENDING
    assert record.attempt == 1
    assert record.started_at is None
    assert record.completed_at is None
    assert record.error_code is None
    assert record.error_message is None
    assert record.created_at is not None


def test_get_by_id_round_trips_step(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        created = repository.create(run_id, RunStage.INGEST, 1)
        connection.commit()

    with pool.connection() as connection:
        retrieved = RunStepRepository(connection).get_by_id(created.step_id)

    assert retrieved == created


def test_get_by_id_returns_none_for_unknown_step() -> None:
    with pool.connection() as connection:
        record = RunStepRepository(connection).get_by_id(str(uuid4()))

    assert record is None


def test_get_for_run_stage_attempt_returns_exact_step(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        expected = repository.create(run_id, RunStage.EXAMINE, 1)
        connection.commit()

        found = repository.get_for_run_stage_attempt(run_id, RunStage.EXAMINE, 1)

    assert found == expected


def test_list_for_run_returns_steps_by_stage_then_attempt(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        expected = [
            repository.create(run_id, RunStage.COMMIT, 1),
            repository.create(run_id, RunStage.INGEST, 1),
            repository.create(run_id, RunStage.EXAMINE, 1),
        ]
        connection.commit()

        listed = repository.list_for_run(run_id)

    assert listed == [
        expected[1],
        expected[2],
        expected[0],
    ]


def test_duplicate_stage_attempt_is_rejected(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)
    create_step(run_id)

    with pool.connection() as connection:
        with pytest.raises(UniqueViolation):
            RunStepRepository(connection).create(run_id, RunStage.INGEST, 1)

        connection.rollback()


def test_unknown_run_is_rejected_by_foreign_key() -> None:
    with pool.connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            RunStepRepository(connection).create(str(uuid4()), RunStage.INGEST, 1)

        connection.rollback()


def test_non_positive_attempt_is_rejected_by_database(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        # The successor trigger runs before the check constraint; either
        # guard correctly rejects an attempt outside the Phase 5C budget.
        with pytest.raises((CheckViolation, RaiseException)):
            RunStepRepository(connection).create(run_id, RunStage.INGEST, 0)

        connection.rollback()


@pytest.mark.parametrize(
    "statuses",
    [
        (RunStepStatus.RUNNING, RunStepStatus.COMPLETED),
        (RunStepStatus.RUNNING, RunStepStatus.SKIPPED),
        (RunStepStatus.RUNNING, RunStepStatus.ESCALATED),
        (RunStepStatus.RUNNING, RunStepStatus.FAILED),
        (
            RunStepStatus.RUNNING,
            RunStepStatus.AWAITING_UPSTREAM,
            RunStepStatus.RUNNING,
        ),
    ],
)
def test_database_allows_valid_status_transition_paths(
    pile_id: str,
    statuses: tuple[RunStepStatus, ...],
) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        record = repository.create(run_id, RunStage.INGEST, 1)
        connection.commit()

        for status in statuses:
            closed = status in {
                RunStepStatus.COMPLETED, RunStepStatus.SKIPPED,
                RunStepStatus.ESCALATED, RunStepStatus.FAILED,
            }
            updated = repository.update_execution_state(
                record.step_id, status,
                mark_started=status is RunStepStatus.RUNNING,
                mark_completed=closed,
            )
            assert updated is not None
            assert updated.status is status
            if closed:
                RunMetricRepository(connection).create_for_closed_step(run_id, updated.stage, updated.attempt)

        connection.commit()


def test_database_rejects_closed_transition_without_timestamps(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        created = repository.create(run_id, RunStage.INGEST, 1)
        connection.commit()

        running = repository.update_status(created.step_id, RunStepStatus.RUNNING)
        same_status = repository.update_status(created.step_id, RunStepStatus.RUNNING)
        with pytest.raises(CheckViolation):
            repository.update_status(created.step_id, RunStepStatus.COMPLETED)
        connection.rollback()

    assert running is not None
    assert running.status is RunStepStatus.RUNNING
    assert same_status is not None
    assert same_status.status is RunStepStatus.RUNNING


def test_update_execution_state_uses_database_timestamps_and_error_metadata(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        created = repository.create(run_id, RunStage.INGEST, 1)
        running = repository.update_execution_state(
            created.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert running is not None
        same_running = repository.update_execution_state(
            created.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert same_running is not None
        completed = repository.update_execution_state(
            created.step_id,
            RunStepStatus.COMPLETED,
            mark_completed=True,
            clear_error=True,
        )
        assert completed is not None
        RunMetricRepository(connection).create_for_closed_step(run_id, completed.stage, completed.attempt)
        connection.commit()

    assert running.started_at is not None
    assert same_running.started_at == running.started_at
    assert completed.completed_at is not None
    assert completed.error_code is None
    assert completed.error_message is None


@pytest.mark.parametrize(
    "terminal_status",
    (RunStepStatus.COMPLETED, RunStepStatus.FAILED, RunStepStatus.ESCALATED),
)
def test_terminal_closure_is_monotonic_against_a_later_started_at(
    pile_id: str,
    terminal_status: RunStepStatus,
) -> None:
    """A pooled transaction cannot make terminal time regress before start."""
    run_id = create_manual_run(pile_id)
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        created = steps.create(run_id, RunStage.UNDERSTAND, 1)
        steps.update_execution_state(created.step_id, RunStepStatus.RUNNING, mark_started=True)
        connection.execute(
            "UPDATE run_steps SET started_at = clock_timestamp() + interval '1 second' WHERE id = %s::uuid",
            (created.step_id,),
        )
        closed = steps.update_execution_state(
            created.step_id,
            terminal_status,
            mark_completed=True,
        )
        assert closed is not None
        metric = RunMetricRepository(connection).create_for_closed_step(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )
        expected = connection.execute(
            """SELECT FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT
               FROM run_steps WHERE id = %s::uuid""",
            (created.step_id,),
        ).fetchone()
        assert closed.started_at is not None and closed.completed_at is not None
        assert closed.completed_at >= closed.started_at
        assert expected is not None and metric.duration_ms == expected[0]
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        connection.rollback()


def test_retrying_closure_is_monotonic_and_uses_persisted_metric_duration(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        predecessor = steps.create(run_id, RunStage.UNDERSTAND, 1)
        steps.update_execution_state(predecessor.step_id, RunStepStatus.RUNNING, mark_started=True)
        connection.execute(
            "UPDATE run_steps SET started_at = clock_timestamp() + interval '1 second' WHERE id = %s::uuid",
            (predecessor.step_id,),
        )
        operation = IdempotencyRepository(connection).claim(
            "fact-extraction-v1",
            f"understand:{run_id}",
        )
        assert operation is not None
        failed = IdempotencyRepository(connection).mark_failed(
            operation.scope,
            operation.key,
            operation.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert failed is not None
        closed = steps.update_execution_state(
            predecessor.step_id,
            RunStepStatus.RETRYING,
            mark_completed=True,
        )
        assert closed is not None
        metric = RunMetricRepository(connection).create_for_closed_step(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )
        steps.create(
            run_id,
            RunStage.UNDERSTAND,
            2,
            retry_operation_scope=operation.scope,
            retry_operation_key=operation.key,
            retry_from_claim_generation=failed.claim_generation,
        )
        expected = connection.execute(
            """SELECT FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT
               FROM run_steps WHERE id = %s::uuid""",
            (predecessor.step_id,),
        ).fetchone()
        assert closed.started_at is not None and closed.completed_at is not None
        assert closed.completed_at >= closed.started_at
        assert expected is not None and metric.duration_ms == expected[0]
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        connection.rollback()


def test_awaiting_upstream_round_trip_preserves_start_and_completion_metadata(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        created = repository.create(run_id, RunStage.INGEST, 1)
        running = repository.update_execution_state(
            created.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert running is not None and running.started_at is not None
        awaiting = repository.update_execution_state(
            created.step_id,
            RunStepStatus.AWAITING_UPSTREAM,
            error_code="UPSTREAM_PENDING",
            error_message="waiting for source readiness",
        )
        assert awaiting is not None
        resumed = repository.update_execution_state(
            created.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert resumed is not None
        completed = repository.update_execution_state(
            created.step_id,
            RunStepStatus.COMPLETED,
            mark_completed=True,
            clear_error=True,
        )
        assert completed is not None
        same_completed = repository.update_execution_state(
            completed.step_id,
            RunStepStatus.COMPLETED,
            mark_completed=True,
            clear_error=True,
        )
        assert same_completed is not None
        RunMetricRepository(connection).create_for_closed_step(run_id, same_completed.stage, same_completed.attempt)
        connection.commit()

    assert awaiting.started_at == running.started_at
    assert awaiting.error_code == "UPSTREAM_PENDING"
    assert resumed.started_at == running.started_at
    assert resumed.error_code is None
    assert completed.completed_at is not None
    assert same_completed is not None
    assert same_completed.completed_at == completed.completed_at


def test_database_rejects_invalid_and_terminal_status_updates(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        pending = repository.create(run_id, RunStage.INGEST, 1)
        terminal = repository.create(run_id, RunStage.UNDERSTAND, 1)
        connection.commit()

        with pytest.raises(RaiseException):
            repository.update_status(pending.step_id, RunStepStatus.SKIPPED)

        connection.rollback()

        assert repository.update_execution_state(
            terminal.step_id, RunStepStatus.RUNNING, mark_started=True,
        ) is not None
        completed = repository.update_execution_state(
            terminal.step_id, RunStepStatus.COMPLETED, mark_completed=True,
        )
        assert completed is not None
        RunMetricRepository(connection).create_for_closed_step(run_id, completed.stage, completed.attempt)
        connection.commit()

        with pytest.raises(RaiseException):
            repository.update_status(terminal.step_id, RunStepStatus.RUNNING)

        connection.rollback()


def test_update_status_returns_none_for_unknown_step() -> None:
    with pool.connection() as connection:
        record = RunStepRepository(connection).update_status(
            str(uuid4()),
            RunStepStatus.RUNNING,
        )

    assert record is None


def test_caller_rollback_removes_uncommitted_step(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        record = repository.create(run_id, RunStage.INGEST, 1)
        connection.rollback()

    with pool.connection() as connection:
        persisted = RunStepRepository(connection).get_by_id(record.step_id)

    assert persisted is None


def test_retry_history_rejects_mutation_or_deletion_that_would_orphan_chain(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        predecessor = steps.create(run_id, RunStage.UNDERSTAND, 1)
        steps.update_execution_state(predecessor.step_id, RunStepStatus.RUNNING, mark_started=True)
        operation = IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{run_id}")
        assert operation is not None
        failed = IdempotencyRepository(connection).mark_failed(
            operation.scope, operation.key, operation.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert failed is not None
        closed = steps.update_execution_state(
            predecessor.step_id, RunStepStatus.RETRYING, mark_completed=True,
            error_code=failed.failure_kind.value if failed.failure_kind else None,
        )
        assert closed is not None
        RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.UNDERSTAND, 1)
        successor = steps.create(
            run_id, RunStage.UNDERSTAND, 2,
            retry_operation_scope=operation.scope,
            retry_operation_key=operation.key,
            retry_from_claim_generation=operation.claim_generation,
        )
        with pytest.raises(RaiseException, match="immutable"), connection.transaction():
            steps.update_execution_state(predecessor.step_id, RunStepStatus.RETRYING)
        with pytest.raises(RaiseException, match="immutable"), connection.transaction():
            connection.execute("DELETE FROM run_steps WHERE id = %s::uuid", (predecessor.step_id,))
        with pytest.raises(RaiseException, match="cannot be removed"), connection.transaction():
            connection.execute("DELETE FROM run_steps WHERE id = %s::uuid", (successor.step_id,))
        connection.rollback()


def test_retry_successor_round_trips_provenance_and_requires_retrying_predecessor(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        first = steps.create(run_id, RunStage.UNDERSTAND, 1)
        steps.update_execution_state(first.step_id, RunStepStatus.RUNNING, mark_started=True)
        operation = IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{run_id}")
        assert operation is not None
        failed = IdempotencyRepository(connection).mark_failed(
            operation.scope,
            operation.key,
            operation.claim_generation,
            IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE,
        )
        assert failed is not None

        with pytest.raises(RaiseException, match="contiguous RETRYING"):
            steps.create(
                run_id,
                RunStage.UNDERSTAND,
                2,
                retry_operation_scope=operation.scope,
                retry_operation_key=operation.key,
                retry_from_claim_generation=operation.claim_generation,
            )
        connection.rollback()

    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        first = steps.create(run_id, RunStage.UNDERSTAND, 1)
        steps.update_execution_state(first.step_id, RunStepStatus.RUNNING, mark_started=True)
        operation = IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{run_id}")
        assert operation is not None
        failed = IdempotencyRepository(connection).mark_failed(
            operation.scope,
            operation.key,
            operation.claim_generation,
            IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE,
        )
        assert failed is not None
        steps.update_execution_state(first.step_id, RunStepStatus.RETRYING, mark_completed=True)
        RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.UNDERSTAND, 1)
        second = steps.create(
            run_id,
            RunStage.UNDERSTAND,
            2,
            retry_operation_scope=operation.scope,
            retry_operation_key=operation.key,
            retry_from_claim_generation=operation.claim_generation,
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        persisted = steps.get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 2)
        assert persisted == second
        assert (
            persisted.retry_operation_scope,
            persisted.retry_operation_key,
            persisted.retry_from_claim_generation,
        ) == (operation.scope, operation.key, operation.claim_generation)
        connection.rollback()


def test_retry_history_rejects_attempt_four_and_retrying_attempt_three(pile_id: str) -> None:
    run_id = create_manual_run(pile_id)
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        # The successor trigger runs before the check constraint; either
        # guard correctly rejects an attempt outside the Phase 5C budget.
        with pytest.raises((CheckViolation, RaiseException)):
            steps.create(
                run_id,
                RunStage.UNDERSTAND,
                4,
                retry_operation_scope="fact-extraction-v1",
                retry_operation_key=f"understand:{run_id}",
                retry_from_claim_generation=1,
            )
        connection.rollback()

    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        # A third attempt must be a successor of a real retry chain; even if
        # it exists, the Phase 5C attempt budget forbids RETRYING again.
        first = steps.create(run_id, RunStage.UNDERSTAND, 1)
        steps.update_execution_state(first.step_id, RunStepStatus.RUNNING, mark_started=True)
        operation = IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{run_id}")
        assert operation is not None
        first_failed = IdempotencyRepository(connection).mark_failed(
            operation.scope, operation.key, operation.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert first_failed is not None
        steps.update_execution_state(first.step_id, RunStepStatus.RETRYING, mark_completed=True)
        RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.UNDERSTAND, 1)
        second = steps.create(
            run_id, RunStage.UNDERSTAND, 2,
            retry_operation_scope=operation.scope,
            retry_operation_key=operation.key,
            retry_from_claim_generation=first_failed.claim_generation,
        )
        steps.update_execution_state(second.step_id, RunStepStatus.RUNNING, mark_started=True)
        reopened = IdempotencyRepository(connection).reopen_failed(
            operation.scope,
            operation.key,
            first_failed.claim_generation,
            first_failed.failure_kind,
        )
        assert reopened is not None
        second_failed = IdempotencyRepository(connection).mark_failed(
            operation.scope, operation.key, reopened.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert second_failed is not None
        steps.update_execution_state(second.step_id, RunStepStatus.RETRYING, mark_completed=True)
        RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.UNDERSTAND, 2)
        third = steps.create(
            run_id, RunStage.UNDERSTAND, 3,
            retry_operation_scope=operation.scope,
            retry_operation_key=operation.key,
            retry_from_claim_generation=second_failed.claim_generation,
        )
        steps.update_execution_state(third.step_id, RunStepStatus.RUNNING, mark_started=True)
        with pytest.raises(CheckViolation):
            steps.update_execution_state(third.step_id, RunStepStatus.RETRYING, mark_completed=True)
        connection.rollback()
