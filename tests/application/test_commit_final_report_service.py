from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from api.application.commit_final_report_service import (
    CommitFinalReportClaimLostError,
    CommitFinalReportCommand,
    CommitFinalReportInProgressError,
    CommitFinalReportIntegrityError,
    CommitFinalReportService,
)
from api.application.finalize_run_service import (
    DirectFreshFinalizationDisabledError,
    FinalizeRunOutputCommand,
    FinalizeRunService,
    PrepareRunOutputCommand,
)
from db.pool import pool
from domain.final_report import FinalReportInput, final_report_input_fingerprint
from domain.run import RunStage, RunStatus, RunStepStatus
from repositories.audit_event_repository import AuditEventRepository
from repositories.idempotency_repository import (
    IdempotencyRepository,
    IdempotencyStatus,
)
from repositories.output_repository import OutputRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from repositories.run_step_repository import RunStepRepository
from services.final_report_renderer import final_report_content_hash, render_final_report
from services.local_artifact_storage import LocalArtifactStorage


@pytest.fixture(scope="module", autouse=True)
def _open_pool() -> None:
    pool.open()


@pytest.fixture
def commit_run() -> str:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"phase6a commit {uuid4()}",)
        ).fetchone()
        assert pile is not None
        run = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'REVIEW_REQUIRED') RETURNING id",
            (str(pile[0]),),
        ).fetchone()
        assert run is not None
        run_id = str(run[0])
        RunRuleScopeRepository(connection).complete_scope(run_id)
        steps = RunStepRepository(connection)
        for stage in (RunStage.INGEST, RunStage.UNDERSTAND, RunStage.EXAMINE, RunStage.REVIEW):
            row = steps.create(run_id, stage, 1)
            steps.update_execution_state(row.step_id, RunStepStatus.RUNNING, mark_started=True)
            steps.update_execution_state(row.step_id, RunStepStatus.COMPLETED, mark_completed=True)
            RunMetricRepository(connection).create_for_closed_step(run_id, stage, 1)
        commit = steps.create(run_id, RunStage.COMMIT, 1)
        steps.update_execution_state(commit.step_id, RunStepStatus.RUNNING, mark_started=True)
        connection.commit()
    # Rule snapshots are immutable historical provenance and the integration
    # database intentionally retains completed fixtures, like the established
    # finalizer suite.  The UUID-qualified records cannot overlap another test.
    yield run_id


def _services(tmp_path: Path) -> tuple[CommitFinalReportService, FinalizeRunService]:
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    finalizer = FinalizeRunService(pool, storage)
    return CommitFinalReportService(pool, storage, finalizer), finalizer


def test_commit_is_the_single_fresh_finalization_path_and_exact_replays(
    tmp_path: Path, commit_run: str,
) -> None:
    service, finalizer = _services(tmp_path)
    prepared = finalizer.prepare(PrepareRunOutputCommand(commit_run))
    expected_content_hash = final_report_content_hash(
        FinalReportInput(render_input=prepared.manifest),
    )
    artifact = finalizer._artifact_storage.publish_bytes(b"attempted direct bypass")
    with pytest.raises(DirectFreshFinalizationDisabledError):
        finalizer.finalize(
            FinalizeRunOutputCommand(
                commit_run, prepared.render_input_fingerprint, artifact.storage_uri,
                artifact.content_hash, "human", "reviewer",
            )
        )

    created = service.commit(CommitFinalReportCommand(commit_run))
    replay = service.commit(CommitFinalReportCommand(commit_run))

    assert created.replayed is False and replay.replayed is True
    assert created.output.content_hash == expected_content_hash
    assert replay.output == created.output
    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(commit_run)
        output = OutputRepository(connection).get_by_run_and_type(commit_run, "FINAL_REPORT")
        audit = AuditEventRepository(connection).list_for_run(commit_run)
        step = RunStepRepository(connection).get_for_run_stage_attempt(commit_run, RunStage.COMMIT, 1)
        metric = RunMetricRepository(connection).get_for_run_stage_attempt(commit_run, RunStage.COMMIT, 1)
        claim = IdempotencyRepository(connection).get("final-report-render-v1", f"commit-final-report:{commit_run}")
    assert run is not None and run.status is RunStatus.COMPLETED
    assert output == created.output
    assert len(audit) == 1
    assert step is not None and step.status is RunStepStatus.COMPLETED
    assert metric is not None
    assert claim is not None and claim.status is IdempotencyStatus.COMPLETED


def test_finalize_service_exposes_no_fresh_commit_primitive(tmp_path: Path) -> None:
    _, finalizer = _services(tmp_path)

    assert not hasattr(finalizer, "commit_finalization")


def test_private_persistence_rejects_valid_but_non_deterministic_artifact(
    tmp_path: Path,
    commit_run: str,
) -> None:
    service, _ = _services(tmp_path)
    command = CommitFinalReportCommand(commit_run)
    claim = service._claim_or_replay(command)
    assert hasattr(claim, "report_input")

    malicious = service._artifact_storage.publish_bytes(
        b"# Valid artifact with the wrong report bytes\n",
    )
    service._artifact_storage.verify(malicious.storage_uri, malicious.content_hash)

    with pytest.raises(
        CommitFinalReportIntegrityError,
        match="not the deterministic report",
    ):
        service._persist(claim, malicious)

    key = f"commit-final-report:{commit_run}"
    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(commit_run)
        output = OutputRepository(connection).get_by_run_and_type(commit_run, "FINAL_REPORT")
        audits = AuditEventRepository(connection).list_for_run(commit_run)
        step = RunStepRepository(connection).get_for_run_stage_attempt(commit_run, RunStage.COMMIT, 1)
        metric = RunMetricRepository(connection).get_for_run_stage_attempt(commit_run, RunStage.COMMIT, 1)
        durable_claim = IdempotencyRepository(connection).get("final-report-render-v1", key)

    assert run is not None and run.status is RunStatus.REVIEW_REQUIRED
    assert output is None
    assert audits == []
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert metric is None
    assert durable_claim is not None and durable_claim.status is IdempotencyStatus.IN_PROGRESS


def test_commit_in_progress_does_not_publish_or_complete(tmp_path: Path, commit_run: str) -> None:
    service, _ = _services(tmp_path)
    key = f"commit-final-report:{commit_run}"
    with pool.connection() as connection:
        claim = IdempotencyRepository(connection).claim("final-report-render-v1", key)
        assert claim is not None
        connection.commit()

    with pytest.raises(CommitFinalReportInProgressError):
        service.commit(CommitFinalReportCommand(commit_run))

    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(commit_run) == []
        run = RunRepository(connection).get_by_id(commit_run)
        step = RunStepRepository(connection).get_for_run_stage_attempt(commit_run, RunStage.COMMIT, 1)
    assert run is not None and run.status is RunStatus.REVIEW_REQUIRED
    assert step is not None and step.status is RunStepStatus.RUNNING


_SCOPE = "final-report-render-v1"


def _claim_key(run_id: str) -> str:
    return f"commit-final-report:{run_id}"


def _backdate_claim(run_id: str) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE idempotency_records
            SET claimed_at = clock_timestamp() - interval '1 hour'
            WHERE scope = %s AND key = %s
            """,
            (_SCOPE, _claim_key(run_id)),
        )
        connection.commit()


def _assert_single_authoritative_completion(run_id: str) -> None:
    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        output = OutputRepository(connection).get_by_run_and_type(run_id, "FINAL_REPORT")
        audits = [
            event
            for event in AuditEventRepository(connection).list_for_run(run_id)
            if event.event_type == "FINAL_OUTPUT_COMMITTED"
        ]
        steps = [
            step
            for step in RunStepRepository(connection).list_for_run(run_id)
            if step.stage is RunStage.COMMIT
        ]
        claim = IdempotencyRepository(connection).get(_SCOPE, _claim_key(run_id))
    assert run is not None and run.status is RunStatus.COMPLETED
    assert output is not None
    assert len(audits) == 1 and audits[0].entity_id == output.output_id
    assert len(steps) == 1 and steps[0].status is RunStepStatus.COMPLETED
    assert claim is not None and claim.status is IdempotencyStatus.COMPLETED


def test_crash_after_claim_before_render_is_reclaimed_and_completes(
    tmp_path: Path,
    commit_run: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "claim-crash")
    finalizer = FinalizeRunService(pool, storage)
    crashed = CommitFinalReportService(pool, storage, finalizer)
    claim = crashed._claim_or_replay(CommitFinalReportCommand(commit_run))
    assert hasattr(claim, "owner_generation") and claim.owner_generation == 1

    _backdate_claim(commit_run)
    restarted = CommitFinalReportService(
        pool,
        storage,
        finalizer,
        stale_claim_after=timedelta(seconds=1),
    )
    result = restarted.commit(CommitFinalReportCommand(commit_run))

    assert result.replayed is False
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get(_SCOPE, _claim_key(commit_run))
    assert durable is not None and durable.claim_generation == 2
    _assert_single_authoritative_completion(commit_run)


def test_crash_after_render_before_publish_is_reclaimed_and_completes(
    tmp_path: Path,
    commit_run: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "render-crash")
    finalizer = FinalizeRunService(pool, storage)
    crashed = CommitFinalReportService(pool, storage, finalizer)
    claim = crashed._claim_or_replay(CommitFinalReportCommand(commit_run))
    rendered = render_final_report(claim.report_input)
    assert rendered

    _backdate_claim(commit_run)
    restarted = CommitFinalReportService(
        pool,
        storage,
        finalizer,
        stale_claim_after=timedelta(seconds=1),
    )
    result = restarted.commit(CommitFinalReportCommand(commit_run))

    assert result.output.content_hash == f"sha256:{sha256(rendered).hexdigest()}"
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get(_SCOPE, _claim_key(commit_run))
    assert durable is not None and durable.claim_generation == 2
    _assert_single_authoritative_completion(commit_run)


def test_crash_after_artifact_publish_before_db_finalization_reclaims_same_artifact(
    tmp_path: Path,
    commit_run: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "publish-crash")
    finalizer = FinalizeRunService(pool, storage)
    crashed = CommitFinalReportService(pool, storage, finalizer)
    claim = crashed._claim_or_replay(CommitFinalReportCommand(commit_run))
    orphan = storage.publish_bytes(render_final_report(claim.report_input))
    with pool.connection() as connection:
        assert OutputRepository(connection).get_by_run_and_type(commit_run, "FINAL_REPORT") is None

    _backdate_claim(commit_run)
    restarted = CommitFinalReportService(
        pool,
        storage,
        finalizer,
        stale_claim_after=timedelta(seconds=1),
    )
    result = restarted.commit(CommitFinalReportCommand(commit_run))

    assert result.output.storage_uri == orphan.storage_uri
    assert result.output.content_hash == orphan.content_hash
    _assert_single_authoritative_completion(commit_run)


def test_crash_after_db_finalization_before_response_replays_exact_completion(
    tmp_path: Path,
    commit_run: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "response-crash")
    finalizer = FinalizeRunService(pool, storage)
    crashed = CommitFinalReportService(pool, storage, finalizer)
    claim = crashed._claim_or_replay(CommitFinalReportCommand(commit_run))
    artifact = storage.publish_bytes(render_final_report(claim.report_input))
    committed = crashed._persist(claim, artifact)

    restarted = CommitFinalReportService(pool, storage, finalizer)
    replay = restarted.commit(CommitFinalReportCommand(commit_run))

    assert committed.replayed is False
    assert replay.replayed is True
    assert replay.output == committed.output
    _assert_single_authoritative_completion(commit_run)


def test_stale_owner_cannot_finalize_after_generation_is_reclaimed(
    tmp_path: Path,
    commit_run: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "stale-owner")
    finalizer = FinalizeRunService(pool, storage)
    owner_one = CommitFinalReportService(pool, storage, finalizer)
    stale_claim = owner_one._claim_or_replay(CommitFinalReportCommand(commit_run))
    stale_artifact = storage.publish_bytes(render_final_report(stale_claim.report_input))
    _backdate_claim(commit_run)

    owner_two = CommitFinalReportService(
        pool,
        storage,
        finalizer,
        stale_claim_after=timedelta(seconds=1),
    )
    replacement_claim = owner_two._claim_or_replay(CommitFinalReportCommand(commit_run))
    assert replacement_claim.owner_generation == stale_claim.owner_generation + 1

    with pytest.raises(CommitFinalReportClaimLostError):
        owner_one._persist(stale_claim, stale_artifact)

    replacement_artifact = storage.publish_bytes(render_final_report(replacement_claim.report_input))
    completed = owner_two._persist(replacement_claim, replacement_artifact)
    assert completed.replayed is False
    _assert_single_authoritative_completion(commit_run)


def test_two_commit_workers_create_exactly_one_authoritative_completion(
    tmp_path: Path,
    commit_run: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "two-workers")
    finalizer = FinalizeRunService(pool, storage)
    service = CommitFinalReportService(pool, storage, finalizer)
    entered_publish = Event()
    allow_publish = Event()
    real_publish = storage.publish_bytes

    def blocked_publish(content: bytes):
        entered_publish.set()
        assert allow_publish.wait(10)
        return real_publish(content)

    monkeypatch.setattr(storage, "publish_bytes", blocked_publish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_worker = executor.submit(service.commit, CommitFinalReportCommand(commit_run))
        assert entered_publish.wait(10)
        with pytest.raises(CommitFinalReportInProgressError):
            service.commit(CommitFinalReportCommand(commit_run))
        allow_publish.set()
        completed = first_worker.result(timeout=10)

    assert completed.replayed is False
    _assert_single_authoritative_completion(commit_run)


@pytest.mark.parametrize("proof_case", ["missing", "malformed"])
def test_output_without_valid_completed_idempotency_proof_is_integrity_failure(
    tmp_path: Path,
    commit_run: str,
    proof_case: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / f"bad-output-proof-{proof_case}")
    finalizer = FinalizeRunService(pool, storage)
    service = CommitFinalReportService(pool, storage, finalizer)
    prepared = finalizer.prepare(PrepareRunOutputCommand(commit_run))
    artifact = storage.publish_bytes(render_final_report(FinalReportInput(render_input=prepared.manifest)))
    with pool.connection() as connection:
        OutputRepository(connection).create(
            commit_run,
            "FINAL_REPORT",
            artifact.storage_uri,
            artifact.content_hash,
        )
        if proof_case == "malformed":
            repository = IdempotencyRepository(connection)
            claim = repository.claim(_SCOPE, _claim_key(commit_run))
            assert claim is not None
            completed = repository.mark_completed(
                _SCOPE,
                _claim_key(commit_run),
                claim.claim_generation,
                "malformed-result-reference",
            )
            assert completed is not None
        connection.commit()

    with pytest.raises(CommitFinalReportIntegrityError):
        service.commit(CommitFinalReportCommand(commit_run))


@pytest.mark.parametrize("missing_proof", ["output", "run_and_commit", "audit"])
def test_completed_idempotency_without_full_completion_proof_is_integrity_failure(
    tmp_path: Path,
    commit_run: str,
    missing_proof: str,
) -> None:
    storage = LocalArtifactStorage(tmp_path / f"missing-proof-{missing_proof}")
    finalizer = FinalizeRunService(pool, storage)
    service = CommitFinalReportService(pool, storage, finalizer)
    prepared = finalizer.prepare(PrepareRunOutputCommand(commit_run))
    report_input = FinalReportInput(render_input=prepared.manifest)
    artifact = storage.publish_bytes(render_final_report(report_input))
    input_fingerprint = final_report_input_fingerprint(report_input)

    with pool.connection() as connection:
        idempotency = IdempotencyRepository(connection)
        claim = idempotency.claim(_SCOPE, _claim_key(commit_run))
        assert claim is not None
        output = None
        if missing_proof != "output":
            output = OutputRepository(connection).create(
                commit_run,
                "FINAL_REPORT",
                artifact.storage_uri,
                artifact.content_hash,
            )
        if missing_proof == "audit":
            assert output is not None
            step = RunStepRepository(connection).get_for_run_stage_attempt(
                commit_run,
                RunStage.COMMIT,
                1,
            )
            assert step is not None
            closed = RunStepRepository(connection).update_execution_state(
                step.step_id,
                RunStepStatus.COMPLETED,
                mark_completed=True,
            )
            assert closed is not None
            RunMetricRepository(connection).create_for_closed_step(commit_run, RunStage.COMMIT, 1)
            RunRepository(connection).update_status(commit_run, RunStatus.COMPLETED)
        completed = idempotency.mark_completed(
            _SCOPE,
            _claim_key(commit_run),
            claim.claim_generation,
            service._reference(commit_run, input_fingerprint, artifact.content_hash),
        )
        assert completed is not None
        connection.commit()

    with pytest.raises(CommitFinalReportIntegrityError):
        service.commit(CommitFinalReportCommand(commit_run))
