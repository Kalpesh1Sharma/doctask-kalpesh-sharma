from uuid import uuid4

import pytest

from db.pool import pool
from domain.run import RunStage, RunStepStatus
from repositories.provider_usage_repository import ProviderUsageRepository, ProviderUsageState
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_step_repository import RunStepRepository


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def test_provider_usage_receipts_are_generation_and_attempt_scoped() -> None:
    with pool.connection() as connection:
        pile = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"usage-{uuid4()}",)).fetchone()
        assert pile is not None
        run = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s, 'MANUAL', 'RUNNING') RETURNING id",
            (pile[0],),
        ).fetchone()
        assert run is not None
        step = RunStepRepository(connection).create(str(run[0]), RunStage.UNDERSTAND, 1)
        RunStepRepository(connection).update_execution_state(step.step_id, RunStepStatus.RUNNING, mark_started=True)
        repository = ProviderUsageRepository(connection)
        event = repository.create_started(
            run_id=str(run[0]), stage=RunStage.UNDERSTAND, attempt=1,
            operation_scope="fact-extraction-v1", operation_key=f"understand:{run[0]}",
            claim_generation=1, provider_name="fake", model_name="fake-v1",
        )
        assert event.state is ProviderUsageState.STARTED
        recorded = repository.record(event.event_id, input_tokens=5, output_tokens=7, estimated_cost=None)
        assert recorded is not None and recorded.state is ProviderUsageState.RECORDED
        replacement = repository.create_started(
            run_id=str(run[0]), stage=RunStage.UNDERSTAND, attempt=1,
            operation_scope="fact-extraction-v1", operation_key=f"understand:{run[0]}",
            claim_generation=2, provider_name="fake", model_name="fake-v2",
        )
        replacement = repository.record(
            replacement.event_id, input_tokens=11, output_tokens=13, estimated_cost=None,
        )
        assert replacement is not None and replacement.claim_generation == 2
        closed = RunStepRepository(connection).update_execution_state(
            step.step_id, RunStepStatus.COMPLETED, mark_completed=True,
        )
        assert closed is not None
        RunMetricRepository(connection).create_for_closed_step(str(run[0]), RunStage.UNDERSTAND, 1)
        events = repository.list_for_step(str(run[0]), RunStage.UNDERSTAND, 1)
        assert [item.claim_generation for item in events] == [1, 2]
        assert [item.model_name for item in events] == ["fake-v1", "fake-v2"]
        assert repository.aggregate_for_step(str(run[0]), RunStage.UNDERSTAND, 1) == (16, 20, None)
        connection.rollback()


def test_unavailable_or_absent_usage_never_fabricates_metric_totals() -> None:
    with pool.connection() as connection:
        pile = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"usage-null-{uuid4()}",)).fetchone()
        assert pile is not None
        run = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s, 'MANUAL', 'RUNNING') RETURNING id",
            (pile[0],),
        ).fetchone()
        assert run is not None
        step = RunStepRepository(connection).create(str(run[0]), RunStage.EXAMINE, 1)
        RunStepRepository(connection).update_execution_state(step.step_id, RunStepStatus.RUNNING, mark_started=True)
        repository = ProviderUsageRepository(connection)
        assert repository.aggregate_for_step(str(run[0]), RunStage.EXAMINE, 1) == (None, None, None)
        event = repository.create_started(
            run_id=str(run[0]), stage=RunStage.EXAMINE, attempt=1,
            operation_scope="contradiction-detection-v1", operation_key=f"examine-contradictions:{run[0]}",
            claim_generation=1, provider_name="fake", model_name=None,
        )
        repository.mark_unavailable(event.event_id)
        assert repository.aggregate_for_step(str(run[0]), RunStage.EXAMINE, 1) == (None, None, None)
        connection.rollback()
