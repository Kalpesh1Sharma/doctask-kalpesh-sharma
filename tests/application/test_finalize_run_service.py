import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.types.json import Jsonb

from api.application import finalize_run_service as finalization_module
from api.application.finalize_run_service import (
    DirectFreshFinalizationDisabledError,
    FinalArtifactIntegrityError,
    FinalizationIntegrityError,
    FinalizeRunNotEligibleError,
    FinalizeRunNotFoundError,
    FinalizeRunOutputCommand,
    FinalizeRunService,
    FinalOutputConflictError,
    InvalidFinalizeRunCommandError,
    PrepareRunOutputCommand,
    RenderInputDriftError,
    ReviewIncompleteError,
)
from db.pool import pool
from domain.finding import Finding, FindingType
from domain.run import RunStage, RunStatus, RunStepStatus
from domain.run_membership import RunMembershipRelation
from repositories.audit_event_repository import AuditEventRepository
from repositories.finding_repository import FindingRepository
from repositories.output_repository import OutputRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from repositories.run_step_repository import RunStepRepository
from services.local_artifact_storage import LocalArtifactStorage


@dataclass(frozen=True)
class FinalizationResources:
    pile_id: str
    run_id: str
    pending_run_id: str
    failed_run_id: str
    uncaptured_run_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> FinalizationResources:
    with pool.connection() as connection:
        pile_id = create_pile(connection)
        run_id = create_run(connection, pile_id, "REVIEW_REQUIRED")
        pending_run_id = create_run(connection, pile_id, "PENDING")
        failed_run_id = create_run(connection, pile_id, "FAILED")
        uncaptured_run_id = create_run(connection, pile_id, "REVIEW_REQUIRED")
        close_empty_scope(connection, run_id)
        close_empty_scope(connection, pending_run_id)
        close_empty_scope(connection, failed_run_id)
        connection.commit()
    return FinalizationResources(pile_id, run_id, pending_run_id, failed_run_id, uncaptured_run_id)


@pytest.fixture
def service(tmp_path: Path) -> FinalizeRunService:
    return FinalizeRunService(pool, LocalArtifactStorage(tmp_path))


def test_prepare_is_deterministic_and_normalizes_uppercase_run_id(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    first = service.prepare(PrepareRunOutputCommand(resources.run_id.upper()))
    second = service.prepare(PrepareRunOutputCommand(resources.run_id))

    assert first.manifest.run_id == resources.run_id
    assert first == second


def test_prepare_rejects_unknown_ineligible_incomplete_and_missing_scope(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    with pool.connection() as connection:
        finding_id = FindingRepository(connection).create(
            Finding(
                finding_id=str(uuid4()),
                origin_run_id=resources.run_id,
                finding_type=FindingType.CONTRADICTION,
                title="Pending review",
                severity="HIGH",
            ),
        ).finding_id
        RunFindingRepository(connection).add(resources.run_id, finding_id, RunMembershipRelation.CREATED)
        connection.commit()

    with pytest.raises(ReviewIncompleteError):
        service.prepare(PrepareRunOutputCommand(resources.run_id))
    with pytest.raises(FinalizeRunNotEligibleError):
        service.prepare(PrepareRunOutputCommand(resources.pending_run_id))
    with pytest.raises(FinalizeRunNotEligibleError):
        service.prepare(PrepareRunOutputCommand(resources.failed_run_id))
    with pytest.raises(FinalizeRunNotFoundError):
        service.prepare(PrepareRunOutputCommand(str(uuid4())))
    with pytest.raises(finalization_module.MissingRunRuleScopeError):
        service.prepare(PrepareRunOutputCommand(resources.uncaptured_run_id))


def test_finalize_rejects_fresh_direct_completion_without_commit_step(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"must use COMMIT")

    with pytest.raises(DirectFreshFinalizationDisabledError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(resources.run_id) == []
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []
        run = RunRepository(connection).get_by_id(resources.run_id)
    assert run is not None
    assert run.status is RunStatus.REVIEW_REQUIRED
    assert run.commit_required is True


def test_finalize_replays_exact_commit_and_rejects_artifact_or_fingerprint_conflicts(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"report replay")
    output, audit = seed_completed_output(
        service,
        resources.run_id,
        prepared.render_input_fingerprint,
        artifact,
    )

    replay = service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    different = service._artifact_storage.publish_bytes(b"different report")

    assert replay.replayed is True
    assert replay.output == output
    assert replay.audit_event == audit
    with pytest.raises(FinalOutputConflictError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, different))
    with pytest.raises(FinalOutputConflictError):
        service.finalize(command(resources.run_id, "sha256:" + "1" * 64, artifact))


def test_replay_artifact_verification_failure_is_an_application_error(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"replay verification")
    seed_completed_output(service, resources.run_id, prepared.render_input_fingerprint, artifact)
    digest = artifact.content_hash.removeprefix("sha256:")
    final_path = service._artifact_storage._root / "final" / "sha256" / digest[:2] / digest
    final_path.unlink()

    with pytest.raises(FinalArtifactIntegrityError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))


def test_fresh_direct_finalize_preserves_read_only_drift_and_artifact_validation(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"read-only validation")

    with pytest.raises(RenderInputDriftError):
        service.finalize(
            FinalizeRunOutputCommand(
                resources.run_id,
                "sha256:" + "9" * 64,
                artifact.storage_uri,
                artifact.content_hash,
                "human",
                "reviewer",
            ),
        )

    with pytest.raises(FinalArtifactIntegrityError):
        service.finalize(
            FinalizeRunOutputCommand(
                resources.run_id,
                prepared.render_input_fingerprint,
                artifact.storage_uri,
                "sha256:" + "0" * 64,
                "human",
                "reviewer",
            ),
        )

    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []


def test_fresh_direct_finalize_never_reaches_audit_write(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"no audit write")
    called = False

    def unexpected_create(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("direct finalization reached audit persistence")

    monkeypatch.setattr(finalization_module.AuditEventRepository, "create", unexpected_create)
    with pytest.raises(DirectFreshFinalizationDisabledError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    assert called is False


def test_fresh_direct_finalize_never_reaches_run_completion(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"no run completion")
    called = False

    def unexpected_status(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("direct finalization reached run completion")

    monkeypatch.setattr(finalization_module.RunRepository, "update_status", unexpected_status)
    with pytest.raises(DirectFreshFinalizationDisabledError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    assert called is False


@pytest.mark.parametrize("case", ["missing", "malformed", "duplicate"])
def test_missing_malformed_or_duplicate_binding_is_never_replayed(
    resources: FinalizationResources,
    service: FinalizeRunService,
    case: str,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"binding integrity")
    output, audit = seed_completed_output(
        service,
        resources.run_id,
        prepared.render_input_fingerprint,
        artifact,
    )

    with pool.connection() as connection:
        if case == "missing":
            connection.execute("DELETE FROM audit_events WHERE id = %s::uuid", (audit.audit_event_id,))
        elif case == "malformed":
            connection.execute(
                "UPDATE audit_events SET payload = %s::jsonb WHERE id = %s::uuid",
                (Jsonb({"not": "a final-output binding"}), audit.audit_event_id),
            )
        else:
            AuditEventRepository(connection).create(
                resources.run_id,
                "output",
                output.output_id,
                "FINAL_OUTPUT_COMMITTED",
                "intruder",
                "intruder-1",
                audit.payload,
            )
        connection.commit()

    with pytest.raises(FinalizationIntegrityError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))


def test_identical_concurrent_fresh_finalizers_are_both_rejected_without_writes(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"concurrent report")
    request = command(resources.run_id, prepared.render_input_fingerprint, artifact)

    def invoke() -> type[BaseException]:
        try:
            service.finalize(request)
        except BaseException as error:  # noqa: BLE001 - test captures competing outcomes
            return type(error)
        raise AssertionError("fresh direct finalization unexpectedly succeeded")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(lambda _: invoke(), range(2)))

    assert results == (DirectFreshFinalizationDisabledError, DirectFreshFinalizationDisabledError)
    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []


def test_different_concurrent_fresh_finalizers_are_both_rejected(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    first_artifact = service._artifact_storage.publish_bytes(b"first report")
    second_artifact = service._artifact_storage.publish_bytes(b"second report")
    requests = (
        command(resources.run_id, prepared.render_input_fingerprint, first_artifact),
        command(resources.run_id, prepared.render_input_fingerprint, second_artifact),
    )

    def invoke(request: FinalizeRunOutputCommand) -> type[BaseException]:
        try:
            service.finalize(request)
        except BaseException as error:  # noqa: BLE001 - test captures competing outcomes
            return type(error)
        raise AssertionError("fresh direct finalization unexpectedly succeeded")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(invoke, requests))

    assert results == (DirectFreshFinalizationDisabledError, DirectFreshFinalizationDisabledError)


def test_direct_finalize_holds_run_lock_until_rejection_then_writer_can_continue(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"lock-only finalization")
    finalizer_locked = threading.Event()
    release_finalizer = threading.Event()
    writer_started = threading.Event()
    original_require_status = service._require_fresh_status

    def pause_after_lock(run):
        original_require_status(run)
        finalizer_locked.set()
        assert release_finalizer.wait(timeout=5)

    def cooperative_writer() -> bool:
        writer_started.set()
        with pool.connection() as connection:
            run = RunRepository(connection).get_for_update(resources.run_id)
            assert run is not None
            connection.commit()
            return run.status is RunStatus.REVIEW_REQUIRED

    monkeypatch.setattr(service, "_require_fresh_status", pause_after_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        finalizer = executor.submit(
            service.finalize,
            command(resources.run_id, prepared.render_input_fingerprint, artifact),
        )
        assert finalizer_locked.wait(timeout=5)
        writer = executor.submit(cooperative_writer)
        assert writer_started.wait(timeout=5)
        time.sleep(0.1)
        assert not writer.done()
        release_finalizer.set()
        with pytest.raises(DirectFreshFinalizationDisabledError):
            finalizer.result(timeout=5)
        assert writer.result(timeout=5) is True


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_errors_restart_full_prepare_transaction(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    original_prepare_once = service._prepare_once
    attempts = 0

    def fail_once(connection, command_value):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retryable_error("retry")
        return original_prepare_once(connection, command_value)

    monkeypatch.setattr(service, "_prepare_once", fail_once)
    result = service.prepare(PrepareRunOutputCommand(resources.run_id))

    assert attempts == 2
    assert result.manifest.run_id == resources.run_id


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_finalize_failure_restarts_then_enforces_direct_cutover(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"retry then cutover")
    original_finalize_once = service._finalize_once
    attempts = 0

    def fail_once(connection, command_value):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retryable_error("retry")
        return original_finalize_once(connection, command_value)

    monkeypatch.setattr(service, "_finalize_once", fail_once)
    with pytest.raises(DirectFreshFinalizationDisabledError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    assert attempts == 2


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_finalize_failure_after_three_attempts_is_reraised(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"retry exhausted")
    attempts = 0

    def always_fail(connection, command_value):
        nonlocal attempts
        attempts += 1
        raise retryable_error("retry exhausted")

    monkeypatch.setattr(service, "_finalize_once", always_fail)
    with pytest.raises(retryable_error):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    assert attempts == 3


@pytest.mark.parametrize("failure_kind", ["direct", "replay_conflict", "replay_integrity"])
def test_finalize_application_errors_are_not_retried(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(f"application error {failure_kind}".encode())
    request = command(resources.run_id, prepared.render_input_fingerprint, artifact)
    expected_error: type[Exception]

    if failure_kind == "direct":
        expected_error = DirectFreshFinalizationDisabledError
    else:
        output, audit = seed_completed_output(
            service,
            resources.run_id,
            prepared.render_input_fingerprint,
            artifact,
        )
        if failure_kind == "replay_conflict":
            different = service._artifact_storage.publish_bytes(b"conflicting artifact")
            request = command(resources.run_id, prepared.render_input_fingerprint, different)
            expected_error = FinalOutputConflictError
        else:
            with pool.connection() as connection:
                connection.execute(
                    "UPDATE audit_events SET payload = %s::jsonb WHERE id = %s::uuid",
                    (Jsonb({"broken": True}), audit.audit_event_id),
                )
                connection.commit()
            assert output.run_id == resources.run_id
            expected_error = FinalizationIntegrityError

    original_finalize_once = service._finalize_once
    attempts = 0

    def count_attempts(connection, command_value):
        nonlocal attempts
        attempts += 1
        return original_finalize_once(connection, command_value)

    monkeypatch.setattr(service, "_finalize_once", count_attempts)
    with pytest.raises(expected_error):
        service.finalize(request)
    assert attempts == 1


def test_finalize_directly_rejects_ineligible_or_incomplete_without_writes(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    canonical_fingerprint = "sha256:" + "4" * 64
    direct_request = FinalizeRunOutputCommand(
        resources.pending_run_id,
        canonical_fingerprint,
        "artifact://sha256/" + "4" * 64,
        canonical_fingerprint,
        "human",
        "reviewer",
    )
    with pytest.raises(FinalizeRunNotEligibleError):
        service.finalize(direct_request)

    with pool.connection() as connection:
        finding_id = FindingRepository(connection).create(
            Finding(
                finding_id=str(uuid4()),
                origin_run_id=resources.run_id,
                finding_type=FindingType.CONTRADICTION,
                title="Finalization pending finding",
                severity="HIGH",
            ),
        ).finding_id
        RunFindingRepository(connection).add(resources.run_id, finding_id, RunMembershipRelation.CREATED)
        connection.commit()

    with pytest.raises(ReviewIncompleteError):
        service.finalize(
            FinalizeRunOutputCommand(
                resources.run_id,
                canonical_fingerprint,
                "artifact://sha256/" + "4" * 64,
                canonical_fingerprint,
                "human",
                "reviewer",
            ),
        )
    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.pending_run_id) == []
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []


def test_invalid_command_is_rejected_before_database_work(service: FinalizeRunService) -> None:
    with pytest.raises(InvalidFinalizeRunCommandError):
        service.finalize(
            FinalizeRunOutputCommand(
                "not-a-uuid",
                "not-a-fingerprint",
                "artifact://sha256/not-used",
                "sha256:" + "0" * 64,
                "human",
                "reviewer",
            ),
        )


def seed_completed_output(service, run_id: str, fingerprint: str, artifact):
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        step = steps.create(run_id, RunStage.COMMIT, 1)
        running = steps.update_execution_state(
            step.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
        )
        assert running is not None
        output = OutputRepository(connection).create(
            run_id,
            "FINAL_REPORT",
            artifact.storage_uri,
            artifact.content_hash,
        )
        payload = {
            "run_id": run_id,
            "output_type": "FINAL_REPORT",
            "render_input_fingerprint": fingerprint,
            "artifact_content_hash": artifact.content_hash,
            "storage_uri": artifact.storage_uri,
        }
        audit = AuditEventRepository(connection).create(
            run_id,
            "output",
            output.output_id,
            "FINAL_OUTPUT_COMMITTED",
            "system",
            "test-seed",
            payload,
        )
        closed = steps.update_execution_state(
            step.step_id,
            RunStepStatus.COMPLETED,
            mark_completed=True,
        )
        assert closed is not None
        RunMetricRepository(connection).create_for_closed_step(
            run_id, RunStage.COMMIT, 1
        )
        run = RunRepository(connection).update_status(run_id, RunStatus.COMPLETED)
        assert run is not None
        connection.commit()
    service._artifact_storage.verify(artifact.storage_uri, artifact.content_hash)
    return output, audit


def create_pile(connection) -> str:
    row = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"Finalize run service {uuid4()}",),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_run(connection, pile_id: str, status: str) -> str:
    row = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, %s::run_status)
        RETURNING id
        """,
        (pile_id, status),
    ).fetchone()
    assert row is not None
    return str(row[0])


def close_empty_scope(connection, run_id: str) -> None:
    RunRuleScopeRepository(connection).complete_scope(run_id)


def command(run_id: str, fingerprint: str, artifact) -> FinalizeRunOutputCommand:
    return FinalizeRunOutputCommand(
        run_id=run_id,
        expected_render_input_fingerprint=fingerprint,
        artifact_storage_uri=artifact.storage_uri,
        artifact_content_hash=artifact.content_hash,
        actor_type="human",
        actor_id="reviewer-1",
    )