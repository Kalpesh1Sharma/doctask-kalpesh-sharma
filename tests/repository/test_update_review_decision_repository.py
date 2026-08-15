from uuid import uuid4

import pytest
from psycopg.errors import RaiseException, UniqueViolation

from db.pool import pool
from domain.finding import Finding, FindingStatus, FindingType
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import UpdateDocumentChange, UpdateDocumentChangeKind
from domain.update_review import UpdateChangeSetManifest, UpdateReviewDecisionType
from repositories.run_finding_repository import RunFindingRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from repositories.update_review_decision_repository import UpdateReviewDecisionRepository


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def fingerprint(character: str) -> str:
    return f"sha256:{character * 64}"


def create_change_set(connection) -> str:
    pile = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"decision-repository-{uuid4()}",),
    ).fetchone()
    assert pile is not None
    document = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
        (str(pile[0]), f"key-{uuid4()}"),
    ).fetchone()
    assert document is not None
    version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        ) VALUES (%s::uuid, 1, %s, %s, 'READY') RETURNING id
        """,
        (str(document[0]), fingerprint("a"), f"storage://{uuid4()}"),
    ).fetchone()
    baseline = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, completed_at)
        VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now()) RETURNING id
        """,
        (str(pile[0]),),
    ).fetchone()
    assert version is not None and baseline is not None
    update = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
        VALUES (%s::uuid, 'UPDATE', 'REVIEW_REQUIRED', %s::uuid, %s::uuid)
        RETURNING id
        """,
        (str(pile[0]), str(version[0]), str(baseline[0])),
    ).fetchone()
    assert update is not None
    manifest = UpdateChangeSetManifest(
        run_id=str(update[0]),
        baseline_run_id=str(baseline[0]),
        trigger_document_version_id=str(version[0]),
        preparation_fingerprint=fingerprint("a"),
        document_delta=(
            UpdateDocumentChange(
                document_id=str(document[0]),
                kind=UpdateDocumentChangeKind.ADDED,
                baseline_document_version_id=None,
                target_document_version_id=str(version[0]),
                target_version_number=1,
                target_content_hash=fingerprint("b"),
            ),
        ),
        fact_delta=(),
        review_state_fingerprint=fingerprint("c"),
        render_input_fingerprint=fingerprint("d"),
    )
    return UpdateChangeSetRepository(connection).create(manifest).change_set_id


def test_decision_repository_uses_immutable_primary_key_and_idempotency_backstops() -> None:
    with pool.connection() as connection:
        change_set_id = create_change_set(connection)
        repository = UpdateReviewDecisionRepository(connection)
        decision = repository.create(
            change_set_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "repository-test",
            f"repository-update-review-{uuid4()}",
        )
        assert repository.get_by_change_set_id(change_set_id) == decision
        assert repository.get_by_idempotency_key(decision.idempotency_key) == decision
        with pytest.raises(UniqueViolation):
            repository.create(
                change_set_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "repository-test",
                f"repository-update-review-{uuid4()}",
            )
        connection.rollback()


def test_decision_idempotency_key_is_unique_across_different_change_sets() -> None:
    with pool.connection() as connection:
        first_change_set_id = create_change_set(connection)
        second_change_set_id = create_change_set(connection)
        repository = UpdateReviewDecisionRepository(connection)
        key = f"cross-change-set-{uuid4()}"
        repository.create(
            first_change_set_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "first",
            key,
        )
        with pytest.raises(UniqueViolation), connection.transaction():
            repository.create(
                second_change_set_id,
                UpdateReviewDecisionType.REJECTED,
                "human",
                "second",
                key,
            )
        connection.rollback()


def test_decision_database_backstops_pending_and_non_reviewable_runs() -> None:
    with pool.connection() as connection:
        change_set_id = create_change_set(connection)
        run_id = connection.execute(
            "SELECT run_id FROM update_change_sets WHERE id = %s::uuid",
            (change_set_id,),
        ).fetchone()
        assert run_id is not None
        finding = Finding(
            finding_id=str(uuid4()),
            origin_run_id=str(run_id[0]),
            finding_type=FindingType.CONTRADICTION,
            rule_id=None,
            title="Pending",
            description=None,
            severity="MEDIUM",
            status=FindingStatus.PENDING,
        )
        from repositories.finding_repository import FindingRepository

        FindingRepository(connection).create(finding)
        RunFindingRepository(connection).add(
            str(run_id[0]),
            finding.finding_id,
            RunMembershipRelation.CREATED,
        )
        with pytest.raises(RaiseException), connection.transaction():
            UpdateReviewDecisionRepository(connection).create(
                change_set_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "pending",
                f"pending-review-{uuid4()}",
            )
        connection.rollback()

    with pool.connection() as connection:
        change_set_id = create_change_set(connection)
        run_id = connection.execute(
            "SELECT run_id FROM update_change_sets WHERE id = %s::uuid",
            (change_set_id,),
        ).fetchone()
        assert run_id is not None
        connection.execute(
            "UPDATE runs SET status = 'COMPLETED', completed_at = now() WHERE id = %s::uuid",
            (str(run_id[0]),),
        )
        with pytest.raises(RaiseException), connection.transaction():
            UpdateReviewDecisionRepository(connection).create(
                change_set_id,
                UpdateReviewDecisionType.APPROVED,
                "human",
                "terminal",
                f"terminal-review-{uuid4()}",
            )
        connection.rollback()


def test_decision_and_referenced_change_set_are_immutable() -> None:
    with pool.connection() as connection:
        change_set_id = create_change_set(connection)
        decision = UpdateReviewDecisionRepository(connection).create(
            change_set_id,
            UpdateReviewDecisionType.APPROVED,
            "human",
            "immutable",
            f"immutable-review-{uuid4()}",
        )
        for statement, parameters in (
            ("UPDATE update_review_decisions SET actor_id = %s WHERE id = %s::uuid", ("other", decision.update_review_decision_id)),
            ("DELETE FROM update_review_decisions WHERE id = %s::uuid", (decision.update_review_decision_id,)),
            ("DELETE FROM update_change_sets WHERE id = %s::uuid", (change_set_id,)),
        ):
            with pytest.raises(RaiseException), connection.transaction():
                connection.execute(statement, parameters)
        connection.rollback()
