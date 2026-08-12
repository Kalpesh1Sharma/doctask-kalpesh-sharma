from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.finding import Finding, FindingType
from domain.review_decision import ReviewDecisionType
from repositories.finding_repository import FindingRepository
from repositories.review_decision_repository import ReviewDecisionRepository


@dataclass(frozen=True)
class ReviewDecisionResources:
    pile_id: str
    run_id: str
    finding_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> ReviewDecisionResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Review decision repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        run_id = create_manual_run(connection, pile_id)
        finding_id = create_finding(connection, run_id)
        connection.commit()

    created_resources = ReviewDecisionResources(pile_id, run_id, finding_id)

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM review_decisions WHERE finding_id = %s::uuid",
                (created_resources.finding_id,),
            )
            connection.execute(
                "DELETE FROM finding_evidence WHERE finding_id = %s::uuid",
                (created_resources.finding_id,),
            )
            connection.execute(
                "DELETE FROM run_findings WHERE finding_id = %s::uuid",
                (created_resources.finding_id,),
            )
            connection.execute("DELETE FROM findings WHERE id = %s::uuid", (created_resources.finding_id,))
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (created_resources.run_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created_resources.pile_id,))
            connection.commit()


def create_manual_run(connection, pile_id: str) -> str:
    run_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO runs (id, pile_id, run_type, status)
        VALUES (%s::uuid, %s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
        """,
        (run_id, pile_id),
    )
    return run_id


def create_finding(connection, run_id: str) -> str:
    finding = Finding(
        finding_id=str(uuid4()),
        origin_run_id=run_id,
        finding_type=FindingType.CONTRADICTION,
        title="Loan amounts conflict",
        severity="HIGH",
    )
    return FindingRepository(connection).create(finding).finding_id


@pytest.mark.parametrize(
    "decision",
    [ReviewDecisionType.APPROVED, ReviewDecisionType.REJECTED],
)
def test_create_get_and_list_persist_review_decision(
    resources: ReviewDecisionResources,
    decision: ReviewDecisionType,
) -> None:
    with pool.connection() as connection:
        repository = ReviewDecisionRepository(connection)
        first = repository.create(
            resources.finding_id,
            decision,
            "human",
            "reviewer-1",
            f"decision-{uuid4()}",
        )
        second = repository.create(
            resources.finding_id,
            decision,
            "human",
            "reviewer-2",
            f"decision-{uuid4()}",
        )
        connection.commit()

    with pool.connection() as connection:
        repository = ReviewDecisionRepository(connection)
        retrieved = repository.get_by_id(first.review_decision_id)
        by_key = repository.get_by_idempotency_key(first.idempotency_key)
        listed = repository.list_for_finding(resources.finding_id)

    assert first.review_decision_id
    assert first.created_at is not None
    assert first.finding_id == resources.finding_id
    assert first.decision is decision
    assert first.actor_type == "human"
    assert first.actor_id == "reviewer-1"
    assert retrieved == first
    assert by_key == first
    assert listed == sorted(
        [first, second],
        key=lambda record: (record.created_at, record.review_decision_id),
    )


def test_unknown_id_and_idempotency_key_return_none(resources: ReviewDecisionResources) -> None:
    with pool.connection() as connection:
        repository = ReviewDecisionRepository(connection)
        missing_id = repository.get_by_id(str(uuid4()))
        missing_key = repository.get_by_idempotency_key(f"unknown-{uuid4()}")

    assert missing_id is None
    assert missing_key is None


def test_duplicate_idempotency_key_is_rejected(resources: ReviewDecisionResources) -> None:
    key = f"decision-{uuid4()}"

    with pool.connection() as connection:
        repository = ReviewDecisionRepository(connection)
        repository.create(
            resources.finding_id,
            ReviewDecisionType.APPROVED,
            "human",
            "reviewer-1",
            key,
        )
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.create(
                resources.finding_id,
                ReviewDecisionType.REJECTED,
                "human",
                "reviewer-2",
                key,
            )

        connection.rollback()


def test_unknown_finding_is_rejected_by_foreign_key(resources: ReviewDecisionResources) -> None:
    with pool.connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            ReviewDecisionRepository(connection).create(
                str(uuid4()),
                ReviewDecisionType.APPROVED,
                "human",
                "reviewer-1",
                f"decision-{uuid4()}",
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_review_decision(resources: ReviewDecisionResources) -> None:
    key = f"decision-{uuid4()}"

    with pool.connection() as connection:
        ReviewDecisionRepository(connection).create(
            resources.finding_id,
            ReviewDecisionType.APPROVED,
            "human",
            "reviewer-1",
            key,
        )
        connection.rollback()

    with pool.connection() as connection:
        persisted = ReviewDecisionRepository(connection).get_by_idempotency_key(key)

    assert persisted is None
