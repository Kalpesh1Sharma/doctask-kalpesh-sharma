from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, RaiseException

from db.pool import pool
from domain.run import RunStage, RunStepStatus
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_step_repository import RunStepRepository


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def _run() -> tuple[str, str]:
    with pool.connection() as connection:
        pile = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"metrics {uuid4()}",)).fetchone()
        assert pile is not None
        run = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s, 'MANUAL', 'PENDING') RETURNING id",
            (pile[0],),
        ).fetchone()
        connection.commit()
    assert run is not None
    return str(pile[0]), str(run[0])


def _cleanup(pile_id: str) -> None:
    with pool.connection() as connection:
        connection.execute("DELETE FROM run_metrics WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (pile_id,))
        connection.execute("DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (pile_id,))
        connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (pile_id,))
        connection.execute("DELETE FROM piles WHERE id = %s::uuid", (pile_id,))
        connection.commit()


def test_metric_is_derived_from_database_step_timestamps() -> None:
    pile_id, run_id = _run()
    try:
        with pool.connection() as connection:
            steps = RunStepRepository(connection)
            step = steps.create(run_id, RunStage.INGEST, 1)
            steps.update_execution_state(step.step_id, RunStepStatus.RUNNING, mark_started=True)
            closed = steps.update_execution_state(step.step_id, RunStepStatus.COMPLETED, mark_completed=True)
            assert closed is not None
            metric = RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.INGEST, 1)
            connection.commit()
        assert metric.duration_ms >= 0
        assert RunMetricRepository(connection).get_for_run_stage_attempt(run_id, RunStage.INGEST, 1) == metric
    finally:
        _cleanup(pile_id)


def test_closed_step_without_metric_is_rejected_at_commit() -> None:
    pile_id, run_id = _run()
    try:
        with pool.connection() as connection:
            steps = RunStepRepository(connection)
            step = steps.create(run_id, RunStage.INGEST, 1)
            steps.update_execution_state(step.step_id, RunStepStatus.RUNNING, mark_started=True)
            steps.update_execution_state(step.step_id, RunStepStatus.COMPLETED, mark_completed=True)
            with pytest.raises(RaiseException, match="requires exactly one metric"):
                connection.commit()
            connection.rollback()
    finally:
        _cleanup(pile_id)


def test_duplicate_metric_create_reuses_one_truthful_duration_and_null_costs() -> None:
    pile_id, run_id = _run()
    try:
        with pool.connection() as connection:
            steps = RunStepRepository(connection)
            step = steps.create(run_id, RunStage.UNDERSTAND, 1)
            steps.update_execution_state(step.step_id, RunStepStatus.RUNNING, mark_started=True)
            steps.update_execution_state(step.step_id, RunStepStatus.COMPLETED, mark_completed=True)
            metrics = RunMetricRepository(connection)
            first = metrics.create_for_closed_step(run_id, RunStage.UNDERSTAND, 1)
            second = metrics.create_for_closed_step(run_id, RunStage.UNDERSTAND, 1)
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            assert first == second
            assert first.duration_ms >= 0
            assert (first.input_tokens, first.output_tokens, first.estimated_cost) == (None, None, None)
            connection.rollback()
    finally:
        _cleanup(pile_id)


def test_database_rejects_metric_for_open_or_unknown_run_step() -> None:
    pile_id, run_id = _run()
    try:
        with pool.connection() as connection:
            RunStepRepository(connection).create(run_id, RunStage.INGEST, 1)
            with pytest.raises(RaiseException, match="closed RunStep"):
                connection.execute(
                    """INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
                       VALUES (%s::uuid, 'INGEST'::run_stage, 1, 0)""",
                    (run_id,),
                )
            connection.rollback()

        with pool.connection() as connection:
            with pytest.raises(ForeignKeyViolation):
                connection.execute(
                    """INSERT INTO run_metrics (run_id, stage, attempt, duration_ms)
                       VALUES (%s::uuid, 'EXAMINE'::run_stage, 1, 0)""",
                    (run_id,),
                )
            connection.rollback()
    finally:
        _cleanup(pile_id)
