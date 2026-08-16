import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, LockNotAvailable, SerializationFailure, UniqueViolation
from psycopg.types.json import Jsonb

from api.application.build_update_change_set_service import (
    BuildUpdateChangeSetCommand,
    BuildUpdateChangeSetService,
    UpdateChangeSetConflictError,
    UpdateChangeSetPreparationError,
    UpdateChangeSetReviewIncompleteError,
    UpdateChangeSetRunNotEligibleError,
    build_current_update_change_set_manifest,
)
from api.application.commit_final_report_service import (
    CommitFinalReportCommand,
    CommitFinalReportService,
)
from api.application.finalize_run_service import (
    DirectFreshFinalizationDisabledError,
    FinalizeRunOutputCommand,
    FinalizeRunService,
    PrepareRunOutputCommand,
    UpdateApprovalMissingError,
    UpdateApprovalRejectedError,
    UpdateApprovalStaleError,
)
from api.application.prepare_update_run_service import (
    PrepareUpdateRunCommand,
    PrepareUpdateRunService,
)
from api.application.review_update_change_set_service import (
    ReviewUpdateChangeSetCommand,
    ReviewUpdateChangeSetService,
    UpdateChangeSetAlreadyReviewedError,
    UpdateChangeSetStaleError,
    UpdateReviewIdempotencyConflictError,
    UpdateReviewIdempotencyKeyError,
    UpdateReviewRunNotEligibleError,
)
from db.pool import pool
from domain.final_report import FinalReportInput
from domain.run import RunStage, RunStatus, RunStepStatus
from domain.update_review import (
    UpdateChangeSetIntegrityError,
    UpdateReviewDecisionType,
)
from repositories.audit_event_repository import AuditEventRepository
from repositories.output_repository import OutputRecord, OutputRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from repositories.run_step_repository import RunStepRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from services.final_report_renderer import render_final_report
from services.local_artifact_storage import LocalArtifactStorage


@dataclass(frozen=True)
class UpdateResources:
    pile_id: str
    baseline_run_id: str
    update_run_id: str
    trigger_document_id: str
    trigger_version_id: str
    retained_document_id: str
    retained_version_id: str
    carried_fact_id: str
    evidence_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def create_prepared_update(*, close_rule_scope: bool = True) -> UpdateResources:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"update-review-{uuid4()}",),
        ).fetchone()
        assert pile is not None
        pile_id = str(pile[0])

        trigger_document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (pile_id, f"trigger-document-{uuid4()}"),
        ).fetchone()
        retained_document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (pile_id, f"retained-document-{uuid4()}"),
        ).fetchone()
        assert trigger_document is not None and retained_document is not None
        trigger_document_id = str(trigger_document[0])
        retained_document_id = str(retained_document[0])

        baseline_version = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            )
            VALUES (%s::uuid, 1, %s, %s, 'READY')
            RETURNING id
            """,
            (trigger_document_id, f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        trigger_version = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            )
            VALUES (%s::uuid, 2, %s, %s, 'READY')
            RETURNING id
            """,
            (trigger_document_id, f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        retained_version = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            )
            VALUES (%s::uuid, 1, %s, %s, 'READY')
            RETURNING id
            """,
            (retained_document_id, f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        assert baseline_version is not None and trigger_version is not None and retained_version is not None
        baseline_version_id = str(baseline_version[0])
        trigger_version_id = str(trigger_version[0])
        retained_version_id = str(retained_version[0])

        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL', 'REVIEW_REQUIRED')
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None
        baseline_run_id = str(baseline[0])
        connection.execute(
            """
            INSERT INTO run_document_versions (run_id, document_version_id)
            VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)
            """,
            (baseline_run_id, baseline_version_id, baseline_run_id, retained_version_id),
        )

        retained_text = "retained clause"
        retained_hash = f"sha256:{sha256(retained_text.encode()).hexdigest()}"
        chunk = connection.execute(
            """
            INSERT INTO document_chunks (
                document_version_id, chunk_index, content, content_hash, start_offset, end_offset
            )
            VALUES (%s::uuid, 0, %s, %s, 0, %s)
            RETURNING id
            """,
            (retained_version_id, retained_text, retained_hash, len(retained_text)),
        ).fetchone()
        assert chunk is not None
        chunk_id = str(chunk[0])
        evidence_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO evidence_spans (
                id, document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash
            )
            VALUES (%s::uuid, %s::uuid, %s::uuid, 0, 8, 'retained', %s)
            """,
            (
                evidence_id,
                retained_version_id,
                chunk_id,
                f"sha256:{sha256(b'retained').hexdigest()}",
            ),
        )
        carried_fact_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO facts (id, origin_run_id, subject, predicate, object_value, status)
            VALUES (%s::uuid, %s::uuid, 'contract', 'notice_days', %s::jsonb, 'CONFIRMED')
            """,
            (carried_fact_id, baseline_run_id, Jsonb({"days": 30})),
        )
        connection.execute(
            """
            INSERT INTO run_facts (run_id, fact_id, relation)
            VALUES (%s::uuid, %s::uuid, 'CREATED')
            """,
            (baseline_run_id, carried_fact_id),
        )
        connection.execute(
            """
            INSERT INTO fact_evidence (fact_id, evidence_span_id)
            VALUES (%s::uuid, %s::uuid)
            """,
            (carried_fact_id, evidence_id),
        )
        RunRuleScopeRepository(connection).complete_scope(baseline_run_id)
        connection.commit()

    activate_commit_step(baseline_run_id)
    with TemporaryDirectory() as baseline_directory:
        baseline_storage = LocalArtifactStorage(Path(baseline_directory))
        baseline_finalizer = FinalizeRunService(pool, baseline_storage)
        CommitFinalReportService(pool, baseline_storage, baseline_finalizer).commit(
            CommitFinalReportCommand(baseline_run_id),
        )

    with pool.connection() as connection:
        update = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, trigger_version_id, baseline_run_id),
        ).fetchone()
        assert update is not None
        update_run_id = str(update[0])
        connection.commit()

    PrepareUpdateRunService(pool).prepare(PrepareUpdateRunCommand(update_run_id))
    with pool.connection() as connection:
        if close_rule_scope:
            RunRuleScopeRepository(connection).complete_scope(update_run_id)
        RunRepository(connection).update_status(update_run_id, RunStatus.REVIEW_REQUIRED)
        connection.commit()
    return UpdateResources(
        pile_id=pile_id,
        baseline_run_id=baseline_run_id,
        update_run_id=update_run_id,
        trigger_document_id=trigger_document_id,
        trigger_version_id=trigger_version_id,
        retained_document_id=retained_document_id,
        retained_version_id=retained_version_id,
        carried_fact_id=carried_fact_id,
        evidence_id=evidence_id,
    )


@pytest.fixture
def reviewable_update() -> UpdateResources:
    return create_prepared_update()


def test_build_review_replay_and_terminal_conflicts(reviewable_update: UpdateResources) -> None:
    builder = BuildUpdateChangeSetService(pool)
    first = builder.build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    replay = builder.build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.change_set.change_set_id == first.change_set.change_set_id

    reviewer = ReviewUpdateChangeSetService(pool)
    key = f"update-review:{reviewable_update.update_run_id}:approve"
    decision = reviewer.record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            key,
        ),
    )
    replayed = reviewer.record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            key,
        ),
    )
    assert decision.replayed is False and replayed.replayed is True
    with pytest.raises(UpdateReviewIdempotencyConflictError):
        reviewer.record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.REJECTED,
                "human",
                "reviewer",
                key,
            ),
        )
    with pytest.raises(UpdateChangeSetAlreadyReviewedError):
        reviewer.record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:second",
            ),
        )
    with pool.connection() as connection:
        events = AuditEventRepository(connection).list_for_run(reviewable_update.update_run_id)
    assert [event.event_type for event in events].count("UPDATE_REVIEW_DECISION_RECORDED") == 1


def test_build_refuses_malformed_stored_identity(reviewable_update: UpdateResources) -> None:
    builder = BuildUpdateChangeSetService(pool)
    with pool.connection() as connection:
        run = RunRepository(connection).get_for_update(reviewable_update.update_run_id)
        assert run is not None
        manifest = build_current_update_change_set_manifest(connection, run)
        value = manifest.model_dump(mode="json")
        connection.execute(
            """
            INSERT INTO update_change_sets (
                run_id, baseline_run_id, trigger_document_version_id,
                preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                manifest, change_set_fingerprint
            )
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                manifest.run_id,
                manifest.baseline_run_id,
                manifest.trigger_document_version_id,
                manifest.preparation_fingerprint,
                manifest.review_state_fingerprint,
                manifest.render_input_fingerprint,
                Jsonb(value),
                "sha256:" + "0" * 64,
            ),
        )
        connection.commit()
    with pytest.raises(UpdateChangeSetIntegrityError):
        builder.build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))


def test_update_finalization_requires_approval_then_finalizes(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    baseline_bytes, baseline_output = materialize_baseline_artifact(storage, reviewable_update)
    finalizer = FinalizeRunService(pool, storage)
    prepared = finalizer.prepare(PrepareRunOutputCommand(reviewable_update.update_run_id))
    artifact = storage.publish_bytes(b"approved update")
    command = FinalizeRunOutputCommand(
        reviewable_update.update_run_id,
        prepared.render_input_fingerprint,
        artifact.storage_uri,
        artifact.content_hash,
        "human",
        "reviewer",
    )
    with pytest.raises(UpdateApprovalMissingError):
        finalizer.finalize(command)

    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    ReviewUpdateChangeSetService(pool).record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            f"update-review:{reviewable_update.update_run_id}:finalize",
        ),
    )
    with pytest.raises(DirectFreshFinalizationDisabledError):
        finalizer.finalize(command)

    activate_commit_step(reviewable_update.update_run_id)
    commit_service = CommitFinalReportService(pool, storage, finalizer)
    result = commit_service.commit(
        CommitFinalReportCommand(
            reviewable_update.update_run_id,
            actor_type="human",
            actor_id="reviewer",
        ),
    )
    replay = commit_service.commit(CommitFinalReportCommand(reviewable_update.update_run_id))
    assert result.replayed is False
    assert replay.replayed is True

    updated_bytes = storage.read_bytes(result.output.storage_uri, result.output.content_hash)
    with pool.connection() as connection:
        audit_events = [
            event
            for event in AuditEventRepository(connection).list_for_run(reviewable_update.update_run_id)
            if event.event_type == "FINAL_OUTPUT_COMMITTED"
        ]
    assert len(audit_events) == 1
    focused_update = audit_events[0].payload.get("focused_update")
    assert isinstance(focused_update, dict)
    assert focused_update["baseline_run_id"] == reviewable_update.baseline_run_id
    assert focused_update["baseline_output_id"] == baseline_output.output_id
    assert focused_update["baseline_content_hash"] == baseline_output.content_hash
    parts = focused_update["preserved_parts"]
    assert isinstance(parts, list) and parts
    part_ids = {part["part_id"] for part in parts}
    assert f"document:{reviewable_update.retained_document_id}" in part_ids
    assert f"fact:{reviewable_update.carried_fact_id}:identity" in part_ids
    assert f"fact:{reviewable_update.carried_fact_id}:claim-body" in part_ids
    assert f"evidence:{reviewable_update.evidence_id}" in part_ids
    assert f"document:{reviewable_update.trigger_document_id}" not in part_ids

    preserved_bytes = 0
    for part in parts:
        baseline_slice = baseline_bytes[part["baseline_start"]:part["baseline_end"]]
        updated_slice = updated_bytes[part["updated_start"]:part["updated_end"]]
        assert baseline_slice == updated_slice
        assert part["content_hash"] == f"sha256:{sha256(baseline_slice).hexdigest()}"
        preserved_bytes += len(updated_slice)
    assert focused_update["preserved_bytes"] == preserved_bytes


def test_rejected_update_cannot_freshly_finalize(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    builder = BuildUpdateChangeSetService(pool)
    builder.build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    ReviewUpdateChangeSetService(pool).record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.REJECTED,
            "human",
            "reviewer",
            f"update-review:{reviewable_update.update_run_id}:reject",
        ),
    )
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    finalizer = FinalizeRunService(pool, storage)
    prepared = finalizer.prepare(PrepareRunOutputCommand(reviewable_update.update_run_id))
    artifact = storage.publish_bytes(b"rejected update")
    with pytest.raises(UpdateApprovalRejectedError):
        finalizer.finalize(
            FinalizeRunOutputCommand(
                reviewable_update.update_run_id,
                prepared.render_input_fingerprint,
                artifact.storage_uri,
                artifact.content_hash,
                "human",
                "reviewer",
            ),
        )
    with pool.connection() as connection:
        count = connection.execute(
            "SELECT count(*) FROM outputs WHERE run_id = %s::uuid",
            (reviewable_update.update_run_id,),
        ).fetchone()
    assert count == (0,)


def test_same_trigger_cannot_create_a_second_update_run(reviewable_update: UpdateResources) -> None:
    with pool.connection() as connection, pytest.raises(UniqueViolation), connection.transaction():
        connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            """,
            (
                reviewable_update.pile_id,
                reviewable_update.trigger_version_id,
                reviewable_update.baseline_run_id,
            ),
        )


def count_rows(run_id: str) -> tuple[int, int, int]:
    with pool.connection() as connection:
        change_sets = connection.execute(
            "SELECT count(*) FROM update_change_sets WHERE run_id = %s::uuid", (run_id,)
        ).fetchone()
        decisions = connection.execute(
            """SELECT count(*) FROM update_review_decisions
               JOIN update_change_sets ON update_change_sets.id = update_review_decisions.change_set_id
               WHERE update_change_sets.run_id = %s::uuid""",
            (run_id,),
        ).fetchone()
        audits = connection.execute(
            "SELECT count(*) FROM audit_events WHERE run_id = %s::uuid", (run_id,)
        ).fetchone()
    assert change_sets is not None and decisions is not None and audits is not None
    return int(change_sets[0]), int(decisions[0]), int(audits[0])


def add_pending_finding(run_id: str) -> str:
    with pool.connection() as connection:
        finding_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO findings (
                id, origin_run_id, finding_type, status, title, severity
            ) VALUES (%s::uuid, %s::uuid, 'CONTRADICTION', 'PENDING', 'Pending', 'MEDIUM')
            """,
            (finding_id, run_id),
        )
        connection.execute(
            """INSERT INTO run_findings (run_id, finding_id, relation)
               VALUES (%s::uuid, %s::uuid, 'CREATED')""",
            (run_id, finding_id),
        )
        connection.commit()
    return finding_id


def add_current_created_fact(run_id: str) -> str:
    with pool.connection() as connection:
        fact_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO facts (id, origin_run_id, subject, predicate, object_value, status)
            VALUES (%s::uuid, %s::uuid, 'new subject', 'new predicate', %s::jsonb, 'CONFIRMED')
            """,
            (fact_id, run_id, Jsonb({"new": True})),
        )
        connection.execute(
            """INSERT INTO run_facts (run_id, fact_id, relation)
               VALUES (%s::uuid, %s::uuid, 'CREATED')""",
            (run_id, fact_id),
        )
        connection.commit()
    return fact_id


def build_and_approve(resources: UpdateResources, token: str = "approve") -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(resources.update_run_id))
    ReviewUpdateChangeSetService(pool).record(
        ReviewUpdateChangeSetCommand(
            resources.update_run_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            f"update-review:{resources.update_run_id}:{token}",
        ),
    )


def activate_commit_step(run_id: str) -> None:
    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        existing = repository.get_for_run_stage_attempt(run_id, RunStage.COMMIT, 1)
        if existing is None:
            existing = repository.create(run_id, RunStage.COMMIT, 1)
        if existing.status is RunStepStatus.PENDING:
            existing = repository.update_execution_state(
                existing.step_id,
                RunStepStatus.RUNNING,
                mark_started=True,
            )
        assert existing is not None and existing.status is RunStepStatus.RUNNING
        connection.commit()


def materialize_baseline_artifact(
    storage: LocalArtifactStorage,
    resources: UpdateResources,
) -> tuple[bytes, OutputRecord]:
    finalizer = FinalizeRunService(pool, storage)
    with pool.connection() as connection:
        baseline_output = OutputRepository(connection).get_by_run_and_type(
            resources.baseline_run_id,
            "FINAL_REPORT",
        )
        assert baseline_output is not None
        manifest = finalizer._build_manifest(connection, resources.baseline_run_id)
    baseline_bytes = render_final_report(FinalReportInput(render_input=manifest))
    artifact = storage.publish_bytes(baseline_bytes)
    assert artifact.storage_uri == baseline_output.storage_uri
    assert artifact.content_hash == baseline_output.content_hash
    return baseline_bytes, baseline_output


def finalize_command(resources: UpdateResources, tmp_path) -> tuple[FinalizeRunService, FinalizeRunOutputCommand]:
    storage = LocalArtifactStorage(tmp_path / f"artifacts-{uuid4()}")
    materialize_baseline_artifact(storage, resources)
    finalizer = FinalizeRunService(pool, storage)
    prepared = finalizer.prepare(PrepareRunOutputCommand(resources.update_run_id))
    artifact = storage.publish_bytes(b"update final artifact")
    return finalizer, FinalizeRunOutputCommand(
        resources.update_run_id,
        prepared.render_input_fingerprint,
        artifact.storage_uri,
        artifact.content_hash,
        "human",
        "reviewer",
    )


def test_build_requires_reviewable_update_and_completed_preparation(
    reviewable_update: UpdateResources,
) -> None:
    with pool.connection() as connection:
        connection.execute(
            "UPDATE runs SET status = 'PENDING' WHERE id = %s::uuid",
            (reviewable_update.update_run_id,),
        )
        connection.commit()
    with pytest.raises(UpdateChangeSetRunNotEligibleError):
        BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    assert count_rows(reviewable_update.update_run_id) == (0, 0, 0)

    other = reviewable_update
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM idempotency_records WHERE scope = %s AND key = %s",
            ("update-preparation-v1", f"prepare-update:{other.update_run_id}"),
        )
        connection.execute(
            "UPDATE runs SET status = 'REVIEW_REQUIRED' WHERE id = %s::uuid",
            (other.update_run_id,),
        )
        connection.commit()
    with pytest.raises(UpdateChangeSetPreparationError):
        BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(other.update_run_id))


def test_build_rejects_incomplete_review_and_valid_current_state_drift(
    reviewable_update: UpdateResources,
) -> None:
    add_pending_finding(reviewable_update.update_run_id)
    with pytest.raises(UpdateChangeSetReviewIncompleteError):
        BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    assert count_rows(reviewable_update.update_run_id) == (0, 0, 0)

    # A separate complete UPDATE can build, then a valid new Fact membership changes
    # the full change-set identity rather than creating another row.
    # The fixture has committed immutable rows, so use a fresh fixture instance through pytest.


def test_build_existing_change_set_conflicts_after_valid_fact_drift(
    reviewable_update: UpdateResources,
) -> None:
    builder = BuildUpdateChangeSetService(pool)
    builder.build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    add_current_created_fact(reviewable_update.update_run_id)
    with pytest.raises(UpdateChangeSetConflictError):
        builder.build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    assert count_rows(reviewable_update.update_run_id)[0] == 1


def test_review_rejects_actor_conflicts_and_key_format_without_duplicate_audit(
    reviewable_update: UpdateResources,
) -> None:
    build_and_approve(reviewable_update)
    reviewer = ReviewUpdateChangeSetService(pool)
    key = f"update-review:{reviewable_update.update_run_id}:approve"
    for actor_type, actor_id in (("service", "reviewer"), ("human", "different")):
        with pytest.raises(UpdateReviewIdempotencyConflictError):
            reviewer.record(
                ReviewUpdateChangeSetCommand(
                    reviewable_update.update_run_id,
                    UpdateReviewDecisionType.APPROVED,
                    actor_type,
                    actor_id,
                    key,
                ),
            )
    with pytest.raises(UpdateReviewIdempotencyKeyError):
        reviewer.record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                "bad-key",
            ),
        )
    with pytest.raises(UpdateReviewIdempotencyKeyError):
        reviewer.record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{uuid4()}:wrong-run",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 1, 1)


def test_review_rejects_incomplete_and_stale_state_without_writes(
    reviewable_update: UpdateResources,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    add_pending_finding(reviewable_update.update_run_id)
    with pytest.raises(UpdateChangeSetReviewIncompleteError):
        ReviewUpdateChangeSetService(pool).record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:pending",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 0, 0)


def test_review_detects_valid_stale_change_set_without_writes(
    reviewable_update: UpdateResources,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    add_current_created_fact(reviewable_update.update_run_id)
    with pytest.raises(UpdateChangeSetStaleError):
        ReviewUpdateChangeSetService(pool).record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:stale",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 0, 0)


def test_review_rejects_fresh_request_after_run_is_no_longer_review_required(
    reviewable_update: UpdateResources,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    with pool.connection() as connection:
        RunRepository(connection).update_status(reviewable_update.update_run_id, RunStatus.FAILED)
        connection.commit()
    with pytest.raises(UpdateReviewRunNotEligibleError):
        ReviewUpdateChangeSetService(pool).record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:terminal",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 0, 0)


def test_review_audit_failure_rolls_back_decision(
    reviewable_update: UpdateResources,
    monkeypatch,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    original_create = AuditEventRepository.create

    def fail_after_insert(self, *args, **kwargs):
        original_create(self, *args, **kwargs)
        raise RuntimeError("audit storage failure")

    monkeypatch.setattr(AuditEventRepository, "create", fail_after_insert)
    with pytest.raises(RuntimeError, match="audit storage failure"):
        ReviewUpdateChangeSetService(pool).record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:audit-fail",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 0, 0)


@pytest.mark.parametrize("error_type", [DeadlockDetected, SerializationFailure])
def test_build_retries_after_staged_change_set_insert(
    reviewable_update: UpdateResources,
    monkeypatch,
    error_type,
) -> None:
    original_create = __import__("repositories.update_change_set_repository", fromlist=["UpdateChangeSetRepository"]).UpdateChangeSetRepository.create
    calls = 0

    def fail_once(self, manifest):
        nonlocal calls
        record = original_create(self, manifest)
        calls += 1
        if calls == 1:
            raise error_type("retry")
        return record

    monkeypatch.setattr(
        "repositories.update_change_set_repository.UpdateChangeSetRepository.create",
        fail_once,
    )
    result = BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    assert result.replayed is False
    assert calls == 2
    assert count_rows(reviewable_update.update_run_id)[0] == 1


@pytest.mark.parametrize("error_type", [DeadlockDetected, SerializationFailure])
def test_build_retry_exhaustion_rolls_back_all_change_sets(
    reviewable_update: UpdateResources,
    monkeypatch,
    error_type,
) -> None:
    from repositories.update_change_set_repository import UpdateChangeSetRepository

    original_create = UpdateChangeSetRepository.create
    calls = 0

    def fail_every_time(self, manifest):
        nonlocal calls
        original_create(self, manifest)
        calls += 1
        raise error_type("retry")

    monkeypatch.setattr(UpdateChangeSetRepository, "create", fail_every_time)
    with pytest.raises(error_type):
        BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    assert calls == 3
    assert count_rows(reviewable_update.update_run_id) == (0, 0, 0)


def test_update_finalizer_rejects_stale_approval_without_output(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    build_and_approve(reviewable_update)
    finalizer, command = finalize_command(reviewable_update, tmp_path)
    add_current_created_fact(reviewable_update.update_run_id)
    with pytest.raises(UpdateApprovalStaleError):
        finalizer.finalize(command)
    with pool.connection() as connection:
        rows = connection.execute(
            """SELECT
                   (SELECT count(*) FROM outputs WHERE run_id = %s::uuid),
                   (SELECT count(*) FROM audit_events WHERE run_id = %s::uuid AND event_type = 'FINAL_OUTPUT_COMMITTED'),
                   (SELECT status FROM runs WHERE id = %s::uuid)""",
            (reviewable_update.update_run_id,) * 3,
        ).fetchone()
    assert rows == (0, 0, "REVIEW_REQUIRED")


def test_update_finalizer_rejects_malformed_change_set_without_output(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    with pool.connection() as connection:
        run = RunRepository(connection).get_for_update(reviewable_update.update_run_id)
        assert run is not None
        manifest = build_current_update_change_set_manifest(connection, run)
        connection.execute(
            """
            INSERT INTO update_change_sets (
                run_id, baseline_run_id, trigger_document_version_id,
                preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                manifest, change_set_fingerprint
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                manifest.run_id,
                manifest.baseline_run_id,
                manifest.trigger_document_version_id,
                manifest.preparation_fingerprint,
                manifest.review_state_fingerprint,
                manifest.render_input_fingerprint,
                Jsonb(manifest.model_dump(mode="json")),
                "sha256:" + "0" * 64,
            ),
        )
        connection.commit()
    finalizer, command = finalize_command(reviewable_update, tmp_path)
    with pytest.raises(UpdateChangeSetIntegrityError):
        finalizer.finalize(command)
    with pool.connection() as connection:
        rows = connection.execute(
            """SELECT
                   (SELECT count(*) FROM outputs WHERE run_id = %s::uuid),
                   (SELECT count(*) FROM audit_events WHERE run_id = %s::uuid AND event_type = 'FINAL_OUTPUT_COMMITTED'),
                   (SELECT status FROM runs WHERE id = %s::uuid)""",
            (reviewable_update.update_run_id,) * 3,
        ).fetchone()
    assert rows == (0, 0, "REVIEW_REQUIRED")


def test_rejected_change_set_second_key_is_terminal_and_write_free(
    reviewable_update: UpdateResources,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    reviewer = ReviewUpdateChangeSetService(pool)
    reviewer.record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.REJECTED,
            "human",
            "reviewer",
            f"update-review:{reviewable_update.update_run_id}:first-rejection",
        ),
    )
    with pytest.raises(UpdateChangeSetAlreadyReviewedError):
        reviewer.record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.REJECTED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:second-rejection",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 1, 1)


@pytest.mark.parametrize("error_type", [DeadlockDetected, SerializationFailure])
def test_review_retries_after_staged_audit_insert(
    reviewable_update: UpdateResources,
    monkeypatch,
    error_type,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    original_create = AuditEventRepository.create
    calls = 0

    def fail_once(self, *args, **kwargs):
        nonlocal calls
        event = original_create(self, *args, **kwargs)
        calls += 1
        if calls == 1:
            raise error_type("retry")
        return event

    monkeypatch.setattr(AuditEventRepository, "create", fail_once)
    result = ReviewUpdateChangeSetService(pool).record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            f"update-review:{reviewable_update.update_run_id}:retry",
        ),
    )
    assert result.replayed is False
    assert calls == 2
    assert count_rows(reviewable_update.update_run_id) == (1, 1, 1)


@pytest.mark.parametrize("error_type", [DeadlockDetected, SerializationFailure])
def test_review_retry_exhaustion_rolls_back_decision_and_audit(
    reviewable_update: UpdateResources,
    monkeypatch,
    error_type,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    original_create = AuditEventRepository.create
    calls = 0

    def fail_every_time(self, *args, **kwargs):
        nonlocal calls
        original_create(self, *args, **kwargs)
        calls += 1
        raise error_type("retry")

    monkeypatch.setattr(AuditEventRepository, "create", fail_every_time)
    with pytest.raises(error_type):
        ReviewUpdateChangeSetService(pool).record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:retry-exhaustion",
            ),
        )
    assert calls == 3
    assert count_rows(reviewable_update.update_run_id) == (1, 0, 0)


def test_build_holds_run_lock_until_change_set_persistence_commits(
    reviewable_update: UpdateResources,
    monkeypatch,
) -> None:
    staged = threading.Event()
    release = threading.Event()
    result: list[object] = []
    original_create = UpdateChangeSetRepository.create

    def pause_after_insert(self, manifest):
        record = original_create(self, manifest)
        staged.set()
        assert release.wait(timeout=5)
        return record

    monkeypatch.setattr(UpdateChangeSetRepository, "create", pause_after_insert)

    def build() -> None:
        result.append(BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id)))

    worker = threading.Thread(target=build)
    worker.start()
    assert staged.wait(timeout=5)
    with pool.connection() as contender, pytest.raises(LockNotAvailable), contender.transaction():
        contender.execute("SET LOCAL lock_timeout = '100ms'")
        assert RunRepository(contender).get_for_update(reviewable_update.update_run_id) is not None
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(result) == 1
    assert count_rows(reviewable_update.update_run_id)[0] == 1


def test_review_holds_run_lock_until_decision_and_audit_commit(
    reviewable_update: UpdateResources,
    monkeypatch,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    staged = threading.Event()
    release = threading.Event()
    result: list[object] = []
    original_create = AuditEventRepository.create

    def pause_after_insert(self, *args, **kwargs):
        event = original_create(self, *args, **kwargs)
        staged.set()
        assert release.wait(timeout=5)
        return event

    monkeypatch.setattr(AuditEventRepository, "create", pause_after_insert)
    command = ReviewUpdateChangeSetCommand(
        reviewable_update.update_run_id,
        UpdateReviewDecisionType.APPROVED,
        "human",
        "reviewer",
        f"update-review:{reviewable_update.update_run_id}:locked-review",
    )

    def review() -> None:
        result.append(ReviewUpdateChangeSetService(pool).record(command))

    worker = threading.Thread(target=review)
    worker.start()
    assert staged.wait(timeout=5)
    with pool.connection() as contender, pytest.raises(LockNotAvailable), contender.transaction():
        contender.execute("SET LOCAL lock_timeout = '100ms'")
        assert RunRepository(contender).get_for_update(reviewable_update.update_run_id) is not None
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(result) == 1
    assert count_rows(reviewable_update.update_run_id) == (1, 1, 1)


def test_build_rejects_otherwise_valid_update_without_closed_rule_scope() -> None:
    resources = create_prepared_update(close_rule_scope=False)
    from api.application.build_update_change_set_service import UpdateChangeSetRuleScopeError

    with pytest.raises(UpdateChangeSetRuleScopeError):
        BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(resources.update_run_id))
    assert count_rows(resources.update_run_id) == (0, 0, 0)


def test_review_rejects_malformed_stored_change_set_before_writes(
    reviewable_update: UpdateResources,
) -> None:
    with pool.connection() as connection:
        run = RunRepository(connection).get_for_update(reviewable_update.update_run_id)
        assert run is not None
        manifest = build_current_update_change_set_manifest(connection, run)
        connection.execute(
            """
            INSERT INTO update_change_sets (
                run_id, baseline_run_id, trigger_document_version_id,
                preparation_fingerprint, review_state_fingerprint, render_input_fingerprint,
                manifest, change_set_fingerprint
            ) VALUES (%s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                manifest.run_id,
                manifest.baseline_run_id,
                manifest.trigger_document_version_id,
                manifest.preparation_fingerprint,
                manifest.review_state_fingerprint,
                manifest.render_input_fingerprint,
                Jsonb(manifest.model_dump(mode="json")),
                "sha256:" + "0" * 64,
            ),
        )
        connection.commit()
    with pytest.raises(UpdateChangeSetIntegrityError):
        ReviewUpdateChangeSetService(pool).record(
            ReviewUpdateChangeSetCommand(
                reviewable_update.update_run_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "reviewer",
                f"update-review:{reviewable_update.update_run_id}:malformed",
            ),
        )
    assert count_rows(reviewable_update.update_run_id) == (1, 0, 0)


def test_exact_review_replay_after_finalization_is_write_free(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    build_and_approve(reviewable_update)
    finalizer, command = finalize_command(reviewable_update, tmp_path)
    with pytest.raises(DirectFreshFinalizationDisabledError):
        finalizer.finalize(command)
    activate_commit_step(reviewable_update.update_run_id)
    CommitFinalReportService(pool, finalizer._artifact_storage, finalizer).commit(
        CommitFinalReportCommand(reviewable_update.update_run_id),
    )
    replay = ReviewUpdateChangeSetService(pool).record(
        ReviewUpdateChangeSetCommand(
            reviewable_update.update_run_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            f"update-review:{reviewable_update.update_run_id}:approve",
        ),
    )
    assert replay.replayed is True
    assert count_rows(reviewable_update.update_run_id) == (1, 1, 2)


def assert_no_fresh_finalization(run_id: str) -> None:
    with pool.connection() as connection:
        rows = connection.execute(
            """SELECT
                   (SELECT count(*) FROM outputs WHERE run_id = %s::uuid),
                   (SELECT count(*) FROM audit_events WHERE run_id = %s::uuid AND event_type = 'FINAL_OUTPUT_COMMITTED'),
                   (SELECT status FROM runs WHERE id = %s::uuid)""",
            (run_id,) * 3,
        ).fetchone()
    assert rows == (0, 0, "REVIEW_REQUIRED")


def test_update_finalizer_rejects_undecided_change_set_without_writes(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(reviewable_update.update_run_id))
    finalizer, command = finalize_command(reviewable_update, tmp_path)
    with pytest.raises(UpdateApprovalMissingError):
        finalizer.finalize(command)
    assert_no_fresh_finalization(reviewable_update.update_run_id)


def test_update_finalizer_missing_and_rejected_paths_leave_fresh_state(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    finalizer, command = finalize_command(reviewable_update, tmp_path)
    with pytest.raises(UpdateApprovalMissingError):
        finalizer.finalize(command)
    assert_no_fresh_finalization(reviewable_update.update_run_id)

    rejected = create_prepared_update()
    BuildUpdateChangeSetService(pool).build(BuildUpdateChangeSetCommand(rejected.update_run_id))
    ReviewUpdateChangeSetService(pool).record(
        ReviewUpdateChangeSetCommand(
            rejected.update_run_id,
            UpdateReviewDecisionType.REJECTED,
            "human",
            "reviewer",
            f"update-review:{rejected.update_run_id}:reject-finalization",
        ),
    )
    rejected_finalizer, rejected_command = finalize_command(rejected, tmp_path)
    with pytest.raises(UpdateApprovalRejectedError):
        rejected_finalizer.finalize(rejected_command)
    assert_no_fresh_finalization(rejected.update_run_id)


def test_completed_update_replays_without_change_set_or_decision(tmp_path) -> None:
    # The true pre-006 FALSE-marker replay is exercised in the clean migration
    # regression.  Here we prove the application replay remains read-only and
    # does not require rebuilding UPDATE approval state once a canonical output
    # already exists.
    resources = create_prepared_update(close_rule_scope=False)
    storage = LocalArtifactStorage(tmp_path / f"historical-artifacts-{uuid4()}")
    artifact = storage.publish_bytes(b"historical UPDATE output")
    expected_fingerprint = "sha256:" + "a" * 64
    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        commit = steps.create(resources.update_run_id, RunStage.COMMIT, 1)
        running = steps.update_execution_state(
            commit.step_id, RunStepStatus.RUNNING, mark_started=True,
        )
        assert running is not None
        output = OutputRepository(connection).create(
            resources.update_run_id,
            "FINAL_REPORT",
            artifact.storage_uri,
            artifact.content_hash,
        )
        AuditEventRepository(connection).create(
            resources.update_run_id,
            "output",
            output.output_id,
            "FINAL_OUTPUT_COMMITTED",
            "human",
            "historical-reviewer",
            {
                "run_id": resources.update_run_id,
                "output_type": "FINAL_REPORT",
                "render_input_fingerprint": expected_fingerprint,
                "artifact_content_hash": artifact.content_hash,
                "storage_uri": artifact.storage_uri,
            },
        )
        closed = steps.update_execution_state(
            commit.step_id, RunStepStatus.COMPLETED, mark_completed=True,
        )
        assert closed is not None
        RunMetricRepository(connection).create_for_closed_step(
            resources.update_run_id,
            RunStage.COMMIT,
            1,
        )
        RunRepository(connection).update_status(resources.update_run_id, RunStatus.COMPLETED)
        connection.commit()
    replay = FinalizeRunService(pool, storage).finalize(
        FinalizeRunOutputCommand(
            resources.update_run_id,
            expected_fingerprint,
            artifact.storage_uri,
            artifact.content_hash,
            "human",
            "historical-reviewer",
        ),
    )
    assert replay.replayed is True
    assert count_rows(resources.update_run_id) == (0, 0, 1)

def test_initial_finalization_is_unchanged_by_update_approval_guard(
    reviewable_update: UpdateResources,
    tmp_path,
) -> None:
    with pool.connection() as connection:
        run = connection.execute(
            """INSERT INTO runs (pile_id, run_type, status)
               VALUES (%s::uuid, 'INITIAL', 'REVIEW_REQUIRED') RETURNING id""",
            (reviewable_update.pile_id,),
        ).fetchone()
        assert run is not None
        run_id = str(run[0])
        RunRuleScopeRepository(connection).complete_scope(run_id)
        connection.commit()
    storage = LocalArtifactStorage(tmp_path / f"initial-artifacts-{uuid4()}")
    service = FinalizeRunService(pool, storage)
    prepared = service.prepare(PrepareRunOutputCommand(run_id))
    artifact = storage.publish_bytes(b"initial output")
    direct_command = FinalizeRunOutputCommand(
        run_id,
        prepared.render_input_fingerprint,
        artifact.storage_uri,
        artifact.content_hash,
        "human",
        "initial-reviewer",
    )
    with pytest.raises(DirectFreshFinalizationDisabledError):
        service.finalize(direct_command)

    activate_commit_step(run_id)
    result = CommitFinalReportService(pool, storage, service).commit(
        CommitFinalReportCommand(run_id, actor_type="human", actor_id="initial-reviewer"),
    )
    assert result.replayed is False
    with pool.connection() as connection:
        completed = RunRepository(connection).get_by_id(run_id)
    assert completed is not None and completed.status is RunStatus.COMPLETED
