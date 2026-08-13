import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, NotNullViolation, SerializationFailure
from psycopg.types.json import Jsonb

from api.application import finalize_run_service as finalization_module
from api.application.finalize_run_service import (
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
from domain.fact import Fact
from domain.finding import Finding, FindingType
from domain.run_membership import RunMembershipRelation
from repositories.audit_event_repository import AuditEventRepository
from repositories.fact_repository import FactRepository
from repositories.finding_repository import FindingRepository
from repositories.output_repository import OutputRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
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


def test_finalize_writes_output_binding_audit_and_completed_run(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"final report")

    result = service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    assert result.replayed is False
    assert result.run.status.value == "COMPLETED"
    assert result.run.completed_at is not None
    assert result.output.output_type == "FINAL_REPORT"
    assert result.audit_event is not None
    assert result.audit_event.entity_type == "output"
    assert result.audit_event.event_type == "FINAL_OUTPUT_COMMITTED"
    assert result.audit_event.payload == {
        "run_id": resources.run_id,
        "output_type": "FINAL_REPORT",
        "render_input_fingerprint": prepared.render_input_fingerprint,
        "artifact_content_hash": artifact.content_hash,
        "storage_uri": artifact.storage_uri,
    }

    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == [result.output]
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == [result.audit_event]


def test_finalize_replays_exact_commit_and_rejects_artifact_or_fingerprint_conflicts(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"report replay")
    first = service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    replay = service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    different = service._artifact_storage.publish_bytes(b"different report")

    assert replay.replayed is True
    assert replay.output == first.output
    assert replay.audit_event == first.audit_event
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
    service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))
    digest = artifact.content_hash.removeprefix("sha256:")
    final_path = service._artifact_storage._root / "final" / "sha256" / digest[:2] / digest
    final_path.unlink()

    with pytest.raises(FinalArtifactIntegrityError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))


def test_finalize_rejects_drift_and_verify_failure_without_writes(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"drift report")
    with pool.connection() as connection:
        fact = FactRepository(connection).create(
            Fact(
                fact_id=str(uuid4()),
                origin_run_id=resources.run_id,
                subject="borrower",
                predicate="name",
                object_value="Ada",
            ),
        )
        RunFactRepository(connection).add(resources.run_id, fact.fact_id, RunMembershipRelation.CREATED)
        connection.commit()

    with pytest.raises(RenderInputDriftError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []
        assert RunRepository(connection).get_by_id(resources.run_id).status.value == "REVIEW_REQUIRED"  # type: ignore[union-attr]

    changed_prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    with pytest.raises(FinalArtifactIntegrityError):
        service.finalize(
            FinalizeRunOutputCommand(
                resources.run_id,
                changed_prepared.render_input_fingerprint,
                artifact.storage_uri,
                "sha256:" + "0" * 64,
                "human",
                "reviewer",
            ),
        )


def test_audit_failure_rolls_back_output_and_run(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"audit failure")
    invalid_actor_command = FinalizeRunOutputCommand(
        resources.run_id,
        prepared.render_input_fingerprint,
        artifact.storage_uri,
        artifact.content_hash,
        None,  # type: ignore[arg-type]
        "reviewer",
    )

    with pytest.raises(NotNullViolation):
        service.finalize(invalid_actor_command)

    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []
        run = RunRepository(connection).get_by_id(resources.run_id)
    assert run is not None
    assert run.status.value == "REVIEW_REQUIRED"


def test_status_failure_rolls_back_output_and_audit(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"status failure")

    def fail_status_update(repository, run_id, status):
        raise NotNullViolation("injected status update failure")

    monkeypatch.setattr(finalization_module.RunRepository, "update_status", fail_status_update)
    with pytest.raises(NotNullViolation):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []


@pytest.mark.parametrize("case", ["missing", "malformed", "duplicate"])
def test_missing_malformed_or_duplicate_binding_is_never_replayed(
    resources: FinalizationResources,
    service: FinalizeRunService,
    case: str,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"binding integrity")
    result = service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    with pool.connection() as connection:
        if case == "missing":
            connection.execute(
                "DELETE FROM audit_events WHERE id = %s::uuid",
                (result.audit_event.audit_event_id,),  # type: ignore[union-attr]
            )
        elif case == "malformed":
            connection.execute(
                "UPDATE audit_events SET payload = %s::jsonb WHERE id = %s::uuid",
                (Jsonb({"not": "a final-output binding"}), result.audit_event.audit_event_id),  # type: ignore[union-attr]
            )
        else:
            AuditEventRepository(connection).create(
                resources.run_id,
                "output",
                result.output.output_id,
                "FINAL_OUTPUT_COMMITTED",
                "intruder",
                "intruder-1",
                result.audit_event.payload,  # type: ignore[union-attr]
            )
        connection.commit()

    with pytest.raises(FinalizationIntegrityError):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))


def test_identical_concurrent_finalizers_produce_winner_and_replay(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"concurrent report")
    request = command(resources.run_id, prepared.render_input_fingerprint, artifact)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.finalize, request)
        second = executor.submit(service.finalize, request)
        results = (first.result(timeout=5), second.result(timeout=5))

    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].output == results[1].output


def test_different_concurrent_finalizers_produce_winner_and_conflict(
    resources: FinalizationResources,
    service: FinalizeRunService,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    first_artifact = service._artifact_storage.publish_bytes(b"first concurrent report")
    second_artifact = service._artifact_storage.publish_bytes(b"second concurrent report")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            service.finalize,
            command(resources.run_id, prepared.render_input_fingerprint, first_artifact),
        )
        second = executor.submit(
            service.finalize,
            command(resources.run_id, prepared.render_input_fingerprint, second_artifact),
        )
        outcomes = (first, second)
        successes = [future.result(timeout=5) for future in outcomes if future.exception(timeout=5) is None]
        failures = [future.exception(timeout=5) for future in outcomes if future.exception(timeout=5) is not None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], FinalOutputConflictError)
    with pool.connection() as connection:
        assert len(OutputRepository(connection).list_for_run(resources.run_id)) == 1
        final_audits = [
            event
            for event in AuditEventRepository(connection).list_for_run(resources.run_id)
            if event.event_type == "FINAL_OUTPUT_COMMITTED"
        ]
    assert len(final_audits) == 1


def test_cooperative_manifest_writer_waits_then_rejects_after_finalization(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"writer contention")
    finalizer_locked = threading.Event()
    release_finalizer = threading.Event()
    writer_started = threading.Event()
    original_build_manifest = service._build_manifest

    def pause_after_lock(connection, run_id):
        manifest = original_build_manifest(connection, run_id)
        finalizer_locked.set()
        assert release_finalizer.wait(timeout=5)
        return manifest

    def cooperative_writer() -> bool:
        writer_started.set()
        with pool.connection() as connection:
            run = RunRepository(connection).get_for_update(resources.run_id)
            assert run is not None
            if run.status.value != "REVIEW_REQUIRED":
                return False
            fact = FactRepository(connection).create(
                Fact(
                    fact_id=str(uuid4()),
                    origin_run_id=resources.run_id,
                    subject="stale",
                    predicate="write",
                    object_value=True,
                ),
            )
            RunFactRepository(connection).add(resources.run_id, fact.fact_id, RunMembershipRelation.CREATED)
            connection.commit()
            return True

    monkeypatch.setattr(service, "_build_manifest", pause_after_lock)
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
        finalizer.result(timeout=5)
        assert writer.result(timeout=5) is False

    with pool.connection() as connection:
        assert RunFactRepository(connection).list_for_run(resources.run_id) == []


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_errors_restart_full_prepare_transaction(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    original_prepare_once = service._prepare_once
    attempts = 0

    def fail_once(connection, command):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retryable_error("retry")
        return original_prepare_once(connection, command)

    monkeypatch.setattr(service, "_prepare_once", fail_once)
    result = service.prepare(PrepareRunOutputCommand(resources.run_id))

    assert attempts == 2
    assert result.manifest.run_id == resources.run_id


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_finalize_failure_after_staging_rolls_back_then_succeeds(
    resources: FinalizationResources,
    service: FinalizeRunService,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    prepared = service.prepare(PrepareRunOutputCommand(resources.run_id))
    artifact = service._artifact_storage.publish_bytes(b"retry after staging")
    original_update_status = finalization_module.RunRepository.update_status
    attempts = 0

    def fail_once_after_staging(repository, run_id, status):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retryable_error("retry after output and audit staging")
        return original_update_status(repository, run_id, status)

    monkeypatch.setattr(finalization_module.RunRepository, "update_status", fail_once_after_staging)
    result = service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    assert attempts == 2
    assert result.replayed is False
    with pool.connection() as connection:
        assert len(OutputRepository(connection).list_for_run(resources.run_id)) == 1
        assert len(AuditEventRepository(connection).list_for_run(resources.run_id)) == 1


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

    def always_fail_after_staging(repository, run_id, status):
        nonlocal attempts
        attempts += 1
        raise retryable_error("retry exhausted")

    monkeypatch.setattr(finalization_module.RunRepository, "update_status", always_fail_after_staging)
    with pytest.raises(retryable_error):
        service.finalize(command(resources.run_id, prepared.render_input_fingerprint, artifact))

    assert attempts == 3
    with pool.connection() as connection:
        assert OutputRepository(connection).list_for_run(resources.run_id) == []
        assert AuditEventRepository(connection).list_for_run(resources.run_id) == []


@pytest.mark.parametrize("failure_kind", ["drift", "artifact", "conflict"])
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
    if failure_kind == "drift":
        request = FinalizeRunOutputCommand(
            request.run_id,
            "sha256:" + "2" * 64,
            request.artifact_storage_uri,
            request.artifact_content_hash,
            request.actor_type,
            request.actor_id,
        )
        expected_error = RenderInputDriftError
    elif failure_kind == "artifact":
        request = FinalizeRunOutputCommand(
            request.run_id,
            request.expected_render_input_fingerprint,
            request.artifact_storage_uri,
            "sha256:" + "3" * 64,
            request.actor_type,
            request.actor_id,
        )
        expected_error = FinalArtifactIntegrityError
    else:
        service.finalize(request)
        request = FinalizeRunOutputCommand(
            request.run_id,
            request.expected_render_input_fingerprint,
            service._artifact_storage.publish_bytes(b"conflicting artifact").storage_uri,
            service._artifact_storage.publish_bytes(b"conflicting artifact").content_hash,
            request.actor_type,
            request.actor_id,
        )
        expected_error = FinalOutputConflictError

    original_finalize_once = service._finalize_once
    attempts = 0

    def count_attempts(connection, command):
        nonlocal attempts
        attempts += 1
        return original_finalize_once(connection, command)

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
