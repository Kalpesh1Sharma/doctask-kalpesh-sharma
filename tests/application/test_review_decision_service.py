import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, SerializationFailure, UniqueViolation

from api.application import review_decision_service as review_module
from api.application.review_decision_service import (
    FindingNotFoundError,
    FindingNotInRunError,
    InvalidReviewIdempotencyKeyError,
    ReviewDecisionCommand,
    ReviewDecisionService,
    ReviewIdempotencyConflictError,
    RunNotFoundError,
    RunNotReviewableError,
    TerminalFindingError,
)
from db.pool import pool
from domain.finding import Finding, FindingStatus, FindingType
from domain.review_decision import ReviewDecisionType
from domain.run_membership import RunMembershipRelation
from repositories.audit_event_repository import AuditEventRepository
from repositories.finding_repository import FindingRepository
from repositories.review_decision_repository import ReviewDecisionRepository
from repositories.run_finding_repository import RunFindingRepository


@dataclass(frozen=True)
class ReviewResources:
    pile_id: str
    review_run_id: str
    second_review_run_id: str
    non_reviewable_run_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> ReviewResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Review decision service test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        review_run_id = create_run(connection, pile_id, "REVIEW_REQUIRED")
        second_review_run_id = create_run(connection, pile_id, "REVIEW_REQUIRED")
        non_reviewable_run_id = create_run(connection, pile_id, "PENDING")
        connection.commit()

    created_resources = ReviewResources(
        pile_id,
        review_run_id,
        second_review_run_id,
        non_reviewable_run_id,
    )

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM review_decisions
                WHERE finding_id IN (
                    SELECT findings.id
                    FROM findings
                    JOIN runs ON runs.id = findings.origin_run_id
                    WHERE runs.pile_id = %s::uuid
                )
                """,
                (created_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM audit_events WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (created_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM run_findings WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (created_resources.pile_id,),
            )
            connection.execute(
                "DELETE FROM findings WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (created_resources.pile_id,),
            )
            connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (created_resources.pile_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created_resources.pile_id,))
            connection.commit()


def create_run(connection, pile_id: str, status: str) -> str:
    run_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO runs (id, pile_id, run_type, status)
        VALUES (%s::uuid, %s::uuid, 'MANUAL'::run_type, %s::run_status)
        """,
        (run_id, pile_id, status),
    )
    return run_id


def create_finding(connection, origin_run_id: str, *membership_run_ids: str) -> str:
    finding = Finding(
        finding_id=str(uuid4()),
        origin_run_id=origin_run_id,
        finding_type=FindingType.CONTRADICTION,
        title="Conflicting effective dates",
        severity="HIGH",
    )
    finding_id = FindingRepository(connection).create(finding).finding_id
    memberships = RunFindingRepository(connection)
    for run_id in membership_run_ids:
        relation = (
            RunMembershipRelation.CREATED
            if run_id == origin_run_id
            else RunMembershipRelation.CARRIED_FORWARD
        )
        memberships.add(run_id, finding_id, relation)
    return finding_id


def command(
    run_id: str,
    finding_id: str,
    *,
    decision: ReviewDecisionType = ReviewDecisionType.APPROVED,
    actor_type: str = "human",
    actor_id: str = "reviewer-1",
    token: str | None = None,
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        run_id=run_id,
        finding_id=finding_id,
        decision=decision,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=f"review:{run_id}:{token or uuid4()}",
    )


def test_approve_reject_and_audit_preserve_independent_decisions(resources: ReviewResources) -> None:
    with pool.connection() as connection:
        approved_finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        rejected_finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        connection.commit()

    service = ReviewDecisionService(pool)
    approved = service.record(command(resources.review_run_id, approved_finding_id))
    rejected = service.record(
        command(
            resources.review_run_id,
            rejected_finding_id,
            decision=ReviewDecisionType.REJECTED,
        ),
    )

    with pool.connection() as connection:
        finding_repository = FindingRepository(connection)
        audits = AuditEventRepository(connection).list_for_run(resources.review_run_id)
        approved_finding = finding_repository.get_by_id(approved_finding_id)
        rejected_finding = finding_repository.get_by_id(rejected_finding_id)

    assert approved.finding.status is FindingStatus.APPROVED
    assert approved.review_complete is False
    assert approved.replayed is False
    assert rejected.finding.status is FindingStatus.REJECTED
    assert rejected.review_complete is True
    assert approved_finding is not None
    assert rejected_finding is not None
    assert approved_finding.status is FindingStatus.APPROVED
    assert rejected_finding.status is FindingStatus.REJECTED
    assert rejected.decision_audit_event is not None
    assert audits[-1].entity_type == "finding"
    assert audits[-1].event_type == "REVIEW_DECISION_RECORDED"
    assert audits[-1].entity_id == rejected_finding_id
    assert audits[-1].payload == {
        "run_id": resources.review_run_id,
        "finding_id": rejected_finding_id,
        "decision": "REJECTED",
        "review_decision_id": rejected.review_decision.review_decision_id,
        "idempotency_key": rejected.review_decision.idempotency_key,
    }


def test_exact_replay_after_terminal_finding_and_non_reviewable_run_is_write_free(
    resources: ReviewResources,
) -> None:
    with pool.connection() as connection:
        finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        connection.commit()

    service = ReviewDecisionService(pool)
    request = command(resources.review_run_id, finding_id)
    first = service.record(request)

    with pool.connection() as connection:
        connection.execute(
            "UPDATE runs SET status = 'FAILED'::run_status WHERE id = %s::uuid",
            (resources.review_run_id,),
        )
        connection.commit()

    replay = service.record(request)

    with pool.connection() as connection:
        decisions = ReviewDecisionRepository(connection).list_for_finding(finding_id)
        audits = AuditEventRepository(connection).list_for_run(resources.review_run_id)

    assert replay.replayed is True
    assert replay.review_decision == first.review_decision
    assert replay.decision_audit_event is None
    assert len(decisions) == 1
    assert len(audits) == 1


def test_fresh_terminal_carried_forward_finding_is_rejected_in_later_run(
    resources: ReviewResources,
) -> None:
    with pool.connection() as connection:
        finding_id = create_finding(
            connection,
            resources.review_run_id,
            resources.review_run_id,
            resources.second_review_run_id,
        )
        connection.commit()

    service = ReviewDecisionService(pool)
    service.record(command(resources.review_run_id, finding_id))

    with pytest.raises(TerminalFindingError):
        service.record(command(resources.second_review_run_id, finding_id))

    with pool.connection() as connection:
        later_decisions = ReviewDecisionRepository(connection).list_for_finding(finding_id)
        later_audits = AuditEventRepository(connection).list_for_run(resources.second_review_run_id)

    assert len(later_decisions) == 1
    assert later_audits == []


def test_conflicting_idempotency_key_is_rejected(resources: ReviewResources) -> None:
    with pool.connection() as connection:
        finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        connection.commit()

    service = ReviewDecisionService(pool)
    request = command(resources.review_run_id, finding_id)
    service.record(request)

    with pytest.raises(ReviewIdempotencyConflictError):
        service.record(
            command(
                resources.review_run_id,
                finding_id,
                actor_id="reviewer-2",
                token=request.idempotency_key.rsplit(":", 1)[1],
            ),
        )


@pytest.mark.parametrize(
    "key",
    [
        "review:not-a-uuid:token",
        "review:00000000-0000-0000-0000-000000000000:contains:semicolon",
        "review:00000000-0000-0000-0000-000000000000:",
        "wrong:00000000-0000-0000-0000-000000000000:token",
    ],
)
def test_invalid_scoped_key_is_rejected_before_database_work(
    resources: ReviewResources,
    key: str,
) -> None:
    request = ReviewDecisionCommand(
        resources.review_run_id,
        str(uuid4()),
        ReviewDecisionType.APPROVED,
        "human",
        "reviewer-1",
        key,
    )

    with pytest.raises(InvalidReviewIdempotencyKeyError):
        ReviewDecisionService(pool).record(request)


def test_run_and_finding_validation_errors_are_distinct(resources: ReviewResources) -> None:
    with pool.connection() as connection:
        non_reviewable_finding_id = create_finding(
            connection,
            resources.non_reviewable_run_id,
            resources.non_reviewable_run_id,
        )
        outside_finding_id = create_finding(
            connection,
            resources.second_review_run_id,
            resources.second_review_run_id,
        )
        connection.commit()

    service = ReviewDecisionService(pool)
    unknown_run_id = str(uuid4())

    with pytest.raises(RunNotFoundError):
        service.record(command(unknown_run_id, str(uuid4())))
    with pytest.raises(RunNotReviewableError):
        service.record(command(resources.non_reviewable_run_id, non_reviewable_finding_id))
    with pytest.raises(FindingNotFoundError):
        service.record(command(resources.review_run_id, str(uuid4())))
    with pytest.raises(FindingNotInRunError):
        service.record(command(resources.review_run_id, outside_finding_id))


def test_postgres_failure_after_finding_update_rolls_back_all_writes(
    resources: ReviewResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pool.connection() as connection:
        finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        conflicting_finding_id = create_finding(
            connection,
            resources.second_review_run_id,
            resources.second_review_run_id,
        )
        connection.commit()

    request = command(resources.review_run_id, finding_id)
    original_create = review_module.ReviewDecisionRepository.create

    def create_conflict_after_update(
        repository,
        finding_id: str,
        decision: ReviewDecisionType,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ):
        with pool.connection() as other_connection:
            other_connection.execute(
                """
                INSERT INTO review_decisions (
                    finding_id, decision, actor_type, actor_id, idempotency_key
                )
                VALUES (%s::uuid, 'APPROVED'::review_decision_type, %s, %s, %s)
                """,
                (conflicting_finding_id, "intruder", "other", idempotency_key),
            )
            other_connection.commit()
        return original_create(
            repository,
            finding_id,
            decision,
            actor_type,
            actor_id,
            idempotency_key,
        )

    monkeypatch.setattr(
        review_module.ReviewDecisionRepository,
        "create",
        create_conflict_after_update,
    )

    with pytest.raises(UniqueViolation):
        ReviewDecisionService(pool).record(request)

    with pool.connection() as connection:
        finding = FindingRepository(connection).get_by_id(finding_id)
        decisions = ReviewDecisionRepository(connection).list_for_finding(finding_id)
        audits = AuditEventRepository(connection).list_for_run(resources.review_run_id)

    assert finding is not None
    assert finding.status is FindingStatus.PENDING
    assert decisions == []
    assert audits == []


def test_same_run_decisions_serialize_on_the_database_run_lock(
    resources: ReviewResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pool.connection() as connection:
        first_finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        second_finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        connection.commit()

    first_updated = threading.Event()
    release_first = threading.Event()
    original_update = review_module.FindingRepository.update_status

    def pause_first_update(repository, finding_id: str, status: FindingStatus):
        record = original_update(repository, finding_id, status)
        if finding_id == first_finding_id:
            first_updated.set()
            assert release_first.wait(timeout=5)
        return record

    monkeypatch.setattr(review_module.FindingRepository, "update_status", pause_first_update)
    service = ReviewDecisionService(pool)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.record, command(resources.review_run_id, first_finding_id))
        assert first_updated.wait(timeout=5)
        second = executor.submit(
            service.record,
            command(
                resources.review_run_id,
                second_finding_id,
                decision=ReviewDecisionType.REJECTED,
            ),
        )
        time.sleep(0.1)
        assert not second.done()
        release_first.set()
        first_result = first.result(timeout=5)
        second_result = second.result(timeout=5)

    assert first_result.finding.status is FindingStatus.APPROVED
    assert second_result.finding.status is FindingStatus.REJECTED


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_database_errors_restart_the_full_transaction(
    resources: ReviewResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    with pool.connection() as connection:
        finding_id = create_finding(connection, resources.review_run_id, resources.review_run_id)
        connection.commit()

    service = ReviewDecisionService(pool)
    original_record_once = service._record_once
    attempts = 0

    def fail_once(connection, request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise retryable_error("retry this transaction")
        return original_record_once(connection, request)

    monkeypatch.setattr(service, "_record_once", fail_once)

    result = service.record(command(resources.review_run_id, finding_id))

    assert attempts == 2
    assert result.replayed is False


def test_non_retryable_database_error_is_not_retried(
    resources: ReviewResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReviewDecisionService(pool)
    attempts = 0

    def fail_once(connection, request):
        nonlocal attempts
        attempts += 1
        raise UniqueViolation("do not retry")

    monkeypatch.setattr(service, "_record_once", fail_once)

    with pytest.raises(UniqueViolation):
        service.record(command(resources.review_run_id, str(uuid4())))

    assert attempts == 1
