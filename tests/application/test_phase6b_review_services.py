from __future__ import annotations

from uuid import uuid4

import pytest

from api.application.acknowledge_run_review_service import (
    AcknowledgeRunReviewService,
    RunReviewAcknowledgementCommand,
)
from api.application.external_review_query_service import ExternalReviewQueryService
from api.application.review_decision_service import (
    ReviewDecisionCommand,
    ReviewDecisionService,
)
from db.pool import pool
from domain.finding import Finding, FindingStatus, FindingType
from domain.review_decision import ReviewDecisionType
from domain.run import RunStage, RunStatus, RunStepStatus
from domain.run_membership import RunMembershipRelation
from repositories.audit_event_repository import AuditEventRepository
from repositories.finding_repository import FindingRepository
from repositories.output_repository import OutputRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRepository
from repositories.run_step_repository import RunStepRepository
from services.local_artifact_storage import LocalArtifactStorage


@pytest.fixture(scope="module", autouse=True)
def _open_pool() -> None:
    pool.open()


@pytest.fixture
def review_pile() -> tuple[str, str]:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Phase 6B-3 review {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        run_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO runs (id, pile_id, run_type, status)
            VALUES (%s::uuid, %s::uuid, 'MANUAL'::run_type, 'REVIEW_REQUIRED'::run_status)
            """,
            (run_id, pile_id),
        )
        connection.commit()
    try:
        yield pile_id, run_id
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM review_decisions
                WHERE finding_id IN (SELECT id FROM findings WHERE origin_run_id = %s::uuid)
                """,
                (run_id,),
            )
            connection.execute("DELETE FROM audit_events WHERE run_id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM run_findings WHERE run_id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM findings WHERE origin_run_id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM run_metrics WHERE run_id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM outputs WHERE run_id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM run_steps WHERE run_id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (run_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (pile_id,))
            connection.commit()


def _create_finding(run_id: str, title: str) -> str:
    with pool.connection() as connection:
        finding = Finding(
            finding_id=str(uuid4()),
            origin_run_id=run_id,
            finding_type=FindingType.CONTRADICTION,
            title=title,
            severity="HIGH",
        )
        created = FindingRepository(connection).create(finding)
        RunFindingRepository(connection).add(
            run_id,
            created.finding_id,
            RunMembershipRelation.CREATED,
        )
        connection.commit()
    return created.finding_id




def test_zero_finding_review_requires_explicit_acknowledgement(
    review_pile: tuple[str, str],
    tmp_path,
) -> None:
    _pile_id, run_id = review_pile
    query = ExternalReviewQueryService(pool, LocalArtifactStorage(tmp_path / "artifacts"))

    gated = query.get_review(run_id)
    assert gated.findings == ()
    assert gated.finding_review_complete is True
    assert gated.run_review_acknowledgement is None
    assert gated.review_complete is False
    assert gated.commit_ready is False

    service = AcknowledgeRunReviewService(pool)
    key = f"run-review:{run_id}:zero-findings"
    first = service.record(
        RunReviewAcknowledgementCommand(
            run_id=run_id,
            actor_type="HUMAN",
            actor_id="reviewer@example.test",
            idempotency_key=key,
        ),
    )
    replay = service.record(
        RunReviewAcknowledgementCommand(
            run_id=run_id,
            actor_type="HUMAN",
            actor_id="reviewer@example.test",
            idempotency_key=key,
        ),
    )

    assert first.replayed is False
    assert replay.replayed is True
    ready = query.get_review(run_id)
    assert ready.run_review_acknowledgement is not None
    assert ready.run_review_acknowledgement.actor_id == "reviewer@example.test"
    assert ready.review_complete is True
    assert ready.commit_ready is True


def test_review_projection_tracks_mixed_item_decisions_and_commit_readiness(
    review_pile: tuple[str, str],
    tmp_path,
) -> None:
    _pile_id, run_id = review_pile
    approved_id = _create_finding(run_id, "Approve me")
    rejected_id = _create_finding(run_id, "Reject me")
    query = ExternalReviewQueryService(pool, LocalArtifactStorage(tmp_path / "artifacts"))

    initial = query.get_review(run_id)
    assert initial.finding_review_complete is False
    assert initial.review_complete is False
    assert initial.commit_ready is False
    assert {item.status for item in initial.findings} == {FindingStatus.PENDING}

    reviewer = ReviewDecisionService(pool)
    reviewer.record(
        ReviewDecisionCommand(
            run_id,
            approved_id,
            ReviewDecisionType.APPROVED,
            "HUMAN",
            "reviewer@example.test",
            f"review:{run_id}:approve",
        ),
    )
    reviewer.record(
        ReviewDecisionCommand(
            run_id,
            rejected_id,
            ReviewDecisionType.REJECTED,
            "HUMAN",
            "reviewer@example.test",
            f"review:{run_id}:reject",
        ),
    )

    completed = query.get_review(run_id)
    assert completed.finding_review_complete is True
    assert completed.review_complete is True
    assert completed.commit_ready is True
    by_id = {item.finding_id: item for item in completed.findings}
    assert by_id[approved_id].status is FindingStatus.APPROVED
    assert by_id[approved_id].decision is not None
    assert by_id[approved_id].decision.decision is ReviewDecisionType.APPROVED
    assert by_id[rejected_id].status is FindingStatus.REJECTED
    assert by_id[rejected_id].decision is not None
    assert by_id[rejected_id].decision.decision is ReviewDecisionType.REJECTED


def test_report_projection_requires_durable_completion_binding_and_returns_verified_bytes(
    review_pile: tuple[str, str],
    tmp_path,
) -> None:
    _pile_id, run_id = review_pile
    storage = LocalArtifactStorage(tmp_path / "artifacts")
    artifact = storage.publish_bytes(b"# Final Report\n\nverified\n")
    render_fingerprint = "sha256:" + "d" * 64

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
        AuditEventRepository(connection).create(
            run_id,
            "output",
            output.output_id,
            "FINAL_OUTPUT_COMMITTED",
            "system",
            "test",
            {
                "run_id": run_id,
                "output_type": "FINAL_REPORT",
                "render_input_fingerprint": render_fingerprint,
                "artifact_content_hash": artifact.content_hash,
                "storage_uri": artifact.storage_uri,
            },
        )
        closed = steps.update_execution_state(
            step.step_id,
            RunStepStatus.COMPLETED,
            mark_completed=True,
        )
        assert closed is not None
        RunMetricRepository(connection).create_for_closed_step(run_id, RunStage.COMMIT, 1)
        completed = RunRepository(connection).update_status(run_id, RunStatus.COMPLETED)
        assert completed is not None
        connection.commit()

    query = ExternalReviewQueryService(pool, storage)
    metadata = query.get_report(run_id)
    replayed_metadata, content = query.read_report_content(run_id)

    assert metadata.output_id == output.output_id
    assert metadata.content_hash == artifact.content_hash
    assert metadata.render_input_fingerprint == render_fingerprint
    assert metadata.focused_update is None
    assert replayed_metadata == metadata
    assert content == b"# Final Report\n\nverified\n"
