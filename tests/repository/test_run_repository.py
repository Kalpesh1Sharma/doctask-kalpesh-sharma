from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, LockNotAvailable, RaiseException, UniqueViolation

from db.pool import pool
from domain.run import Run, RunStage, RunStatus, RunType
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRepository


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
            (f"Run repository test {uuid4()}",),
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
                    SELECT id FROM runs WHERE pile_id = %s::uuid
                )
                """,
                (created_pile_id,),
            )
            connection.execute(
                """
                DELETE FROM outputs
                WHERE run_id IN (
                    SELECT id FROM runs WHERE pile_id = %s::uuid
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


def make_run(
    pile_id: str,
    *,
    run_type: RunType = RunType.MANUAL,
    status: RunStatus = RunStatus.PENDING,
) -> Run:
    return Run(
        run_id=str(uuid4()),
        pile_id=pile_id,
        run_type=run_type,
        status=status,
    )


def create_run(pile_id: str, **overrides: object) -> Run:
    run = make_run(pile_id, **overrides)

    with pool.connection() as connection:
        RunRepository(connection).create(run)
        connection.commit()

    return run


def test_create_persists_run_and_returns_record(pile_id: str) -> None:
    run = make_run(pile_id)

    with pool.connection() as connection:
        record = RunRepository(connection).create(run)
        connection.commit()

    assert record.run_id == run.run_id
    assert record.pile_id == run.pile_id
    assert record.run_type is run.run_type
    assert record.status is run.status
    assert record.trigger_document_version_id is None
    assert record.parent_run_id is None
    assert record.baseline_run_id is None
    assert record.blocked_on_run_id is None
    assert record.commit_required is True
    assert record.started_at is None
    assert record.completed_at is None
    assert record.created_at is not None
    assert record.updated_at is not None
    assert record.to_domain() == run


def test_get_by_id_round_trips_persisted_run(pile_id: str) -> None:
    run = create_run(pile_id)

    with pool.connection() as connection:
        record = RunRepository(connection).get_by_id(run.run_id)

    assert record is not None
    assert record.to_domain() == run


def test_get_by_id_returns_none_for_unknown_run() -> None:
    with pool.connection() as connection:
        record = RunRepository(connection).get_by_id(str(uuid4()))

    assert record is None


def test_get_for_update_returns_run(pile_id: str) -> None:
    run = create_run(pile_id)

    with pool.connection() as connection:
        record = RunRepository(connection).get_for_update(run.run_id)
        connection.commit()

    assert record is not None
    assert record.to_domain() == run


def test_get_for_update_holds_lock_until_caller_commits(pile_id: str) -> None:
    run = create_run(pile_id)

    with pool.connection() as first_connection, pool.connection() as second_connection:
        first_repository = RunRepository(first_connection)
        second_repository = RunRepository(second_connection)

        assert first_repository.get_for_update(run.run_id) is not None
        second_connection.execute("SET LOCAL lock_timeout = '100ms'")

        with pytest.raises(LockNotAvailable):
            second_repository.get_for_update(run.run_id)

        second_connection.rollback()
        first_connection.commit()

        assert second_repository.get_for_update(run.run_id) is not None
        second_connection.commit()


def test_update_status_updates_normal_status(pile_id: str) -> None:
    run = create_run(pile_id)

    with pool.connection() as connection:
        record = RunRepository(connection).update_status(run.run_id, RunStatus.RUNNING)
        connection.commit()

    assert record is not None
    assert record.status is RunStatus.RUNNING
    assert record.completed_at is None


def test_update_status_rejects_completion_without_commit_proof(pile_id: str) -> None:
    run = create_run(pile_id)

    with pool.connection() as connection:
        repository = RunRepository(connection)
        repository.update_status(run.run_id, RunStatus.REVIEW_REQUIRED)
        with pytest.raises(RaiseException, match="Run completion requires FINAL_REPORT output"):
            repository.update_status(run.run_id, RunStatus.COMPLETED)
        connection.rollback()


def test_update_status_sets_completed_at_with_commit_proof(pile_id: str) -> None:
    run = create_run(pile_id)

    with pool.connection() as connection:
        repository = RunRepository(connection)
        repository.update_status(run.run_id, RunStatus.REVIEW_REQUIRED)
        _add_commit_completion_proof(connection, run.run_id)
        record = repository.update_status(run.run_id, RunStatus.COMPLETED)
        connection.commit()

    assert record is not None
    assert record.status is RunStatus.COMPLETED
    assert record.completed_at is not None


def test_update_status_returns_none_for_unknown_run() -> None:
    with pool.connection() as connection:
        record = RunRepository(connection).update_status(str(uuid4()), RunStatus.RUNNING)

    assert record is None


def test_create_completed_run_is_rejected_by_database(pile_id: str) -> None:
    run = make_run(pile_id, status=RunStatus.COMPLETED)

    with pool.connection() as connection:
        with pytest.raises(CheckViolation):
            RunRepository(connection).create(run)

        connection.rollback()


@pytest.mark.parametrize("terminal_status", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_database_rejects_terminal_run_status_regression(
    pile_id: str,
    terminal_status: RunStatus,
) -> None:
    run = create_run(pile_id)

    with pool.connection() as connection:
        repository = RunRepository(connection)
        if terminal_status is RunStatus.COMPLETED:
            repository.update_status(run.run_id, RunStatus.REVIEW_REQUIRED)
            _add_commit_completion_proof(connection, run.run_id)
        terminal_record = repository.update_status(run.run_id, terminal_status)
        connection.commit()

        assert terminal_record is not None

        with pytest.raises(RaiseException):
            repository.update_status(run.run_id, RunStatus.RUNNING)

        connection.rollback()


def _add_commit_completion_proof(connection, run_id: str) -> None:
    connection.execute(
        """
        INSERT INTO run_steps (run_id, stage, status, attempt, started_at)
        VALUES (%s::uuid, 'COMMIT'::run_stage, 'RUNNING'::run_step_status, 1, clock_timestamp())
        """,
        (run_id,),
    )
    connection.execute(
        """
        INSERT INTO outputs (run_id, output_type, storage_uri, content_hash)
        VALUES (%s::uuid, 'FINAL_REPORT', %s, %s)
        """,
        (run_id, f"artifact://{uuid4()}", f"sha256:{uuid4().hex}{uuid4().hex}"),
    )
    connection.execute(
        """
        UPDATE run_steps
        SET status = 'COMPLETED'::run_step_status, completed_at = clock_timestamp()
        WHERE run_id = %s::uuid AND stage = 'COMMIT'::run_stage AND attempt = 1
        """,
        (run_id,),
    )
    RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.COMMIT, 1)


def test_database_rejects_second_initial_run_for_pile(pile_id: str) -> None:
    first_run = make_run(pile_id, run_type=RunType.INITIAL)
    second_run = make_run(pile_id, run_type=RunType.INITIAL)

    with pool.connection() as connection:
        repository = RunRepository(connection)
        repository.create(first_run)
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.create(second_run)

        connection.rollback()
