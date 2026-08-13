from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.finding import Finding, FindingStatus, FindingType
from domain.run_membership import RunMembershipRelation
from repositories.finding_repository import FindingRepository
from repositories.run_finding_repository import RunFindingRepository


@dataclass(frozen=True)
class RunFindingResources:
    pile_id: str
    origin_run_id: str
    carried_run_id: str
    empty_run_id: str
    finding_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> RunFindingResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Run finding repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        origin_run_id = create_manual_run(connection, pile_id)
        carried_run_id = create_manual_run(connection, pile_id)
        empty_run_id = create_manual_run(connection, pile_id)
        finding_id = create_finding(connection, origin_run_id)
        connection.commit()

    created_resources = RunFindingResources(
        pile_id,
        origin_run_id,
        carried_run_id,
        empty_run_id,
        finding_id,
    )

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM finding_evidence
                WHERE finding_id IN (
                    SELECT id FROM findings WHERE origin_run_id = %s::uuid
                )
                """,
                (created_resources.origin_run_id,),
            )
            connection.execute(
                "DELETE FROM run_findings WHERE run_id IN (%s::uuid, %s::uuid, %s::uuid)",
                (
                    created_resources.origin_run_id,
                    created_resources.carried_run_id,
                    created_resources.empty_run_id,
                ),
            )
            connection.execute(
                "DELETE FROM findings WHERE origin_run_id = %s::uuid",
                (created_resources.origin_run_id,),
            )
            connection.execute(
                "DELETE FROM runs WHERE id IN (%s::uuid, %s::uuid, %s::uuid)",
                (
                    created_resources.origin_run_id,
                    created_resources.carried_run_id,
                    created_resources.empty_run_id,
                ),
            )
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


def create_finding(
    connection,
    origin_run_id: str,
    status: FindingStatus = FindingStatus.PENDING,
) -> str:
    finding = Finding(
        finding_id=str(uuid4()),
        origin_run_id=origin_run_id,
        finding_type=FindingType.CONTRADICTION,
        title="Loan amounts conflict",
        severity="HIGH",
        status=status,
    )
    return FindingRepository(connection).create(finding).finding_id


def test_created_and_carried_forward_memberships_preserve_finding_origin(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        created = repository.add(
            resources.origin_run_id,
            resources.finding_id,
            RunMembershipRelation.CREATED,
        )
        carried = repository.add(
            resources.carried_run_id,
            resources.finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        retrieved = repository.get(resources.origin_run_id, resources.finding_id)
        listed = repository.list_for_run(resources.carried_run_id)
        finding = FindingRepository(connection).get_by_id(resources.finding_id)

    assert created.relation is RunMembershipRelation.CREATED
    assert carried.relation is RunMembershipRelation.CARRIED_FORWARD
    assert retrieved == created
    assert listed == [carried]
    assert finding is not None
    assert finding.origin_run_id == resources.origin_run_id


def test_list_for_run_is_deterministic_and_unknown_membership_is_none(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        second_finding_id = create_finding(connection, resources.origin_run_id)
        repository = RunFindingRepository(connection)
        first = repository.add(
            resources.carried_run_id,
            resources.finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        second = repository.add(
            resources.carried_run_id,
            second_finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        listed = repository.list_for_run(resources.carried_run_id)
        missing = repository.get(resources.origin_run_id, second_finding_id)

    assert listed == sorted([first, second], key=lambda record: (record.created_at, record.finding_id))
    assert missing is None


def test_duplicate_membership_is_rejected(resources: RunFindingResources) -> None:
    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        repository.add(
            resources.origin_run_id,
            resources.finding_id,
            RunMembershipRelation.CREATED,
        )
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.add(
                resources.origin_run_id,
                resources.finding_id,
                RunMembershipRelation.CREATED,
            )

        connection.rollback()


def test_unknown_foreign_keys_are_rejected(resources: RunFindingResources) -> None:
    with pool.connection() as connection:
        repository = RunFindingRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.add(
                str(uuid4()),
                resources.finding_id,
                RunMembershipRelation.CREATED,
            )

        connection.rollback()

        with pytest.raises(ForeignKeyViolation):
            repository.add(
                resources.origin_run_id,
                str(uuid4()),
                RunMembershipRelation.CREATED,
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_membership(resources: RunFindingResources) -> None:
    with pool.connection() as connection:
        RunFindingRepository(connection).add(
            resources.origin_run_id,
            resources.finding_id,
            RunMembershipRelation.CREATED,
        )
        connection.rollback()

    with pool.connection() as connection:
        record = RunFindingRepository(connection).get(resources.origin_run_id, resources.finding_id)

    assert record is None


def test_has_pending_for_run_finds_direct_pending_membership(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        repository.add(
            resources.origin_run_id,
            resources.finding_id,
            RunMembershipRelation.CREATED,
        )
        connection.commit()

        has_pending = repository.has_pending_for_run(resources.origin_run_id)

    assert has_pending is True


def test_has_pending_for_run_finds_carried_forward_pending_membership(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        repository.add(
            resources.carried_run_id,
            resources.finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

        has_pending = repository.has_pending_for_run(resources.carried_run_id)

    assert has_pending is True


def test_has_pending_for_run_ignores_resolved_memberships(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        resolved_finding_id = create_finding(
            connection,
            resources.origin_run_id,
            status=FindingStatus.APPROVED,
        )
        repository = RunFindingRepository(connection)
        repository.add(
            resources.carried_run_id,
            resolved_finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

        has_pending = repository.has_pending_for_run(resources.carried_run_id)

    assert has_pending is False


def test_has_pending_for_run_returns_false_for_known_empty_and_unknown_runs(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        known_empty = repository.has_pending_for_run(resources.empty_run_id)
        unknown = repository.has_pending_for_run(str(uuid4()))

    assert known_empty is False
    assert unknown is False


def test_list_review_state_for_run_joins_current_finding_state_deterministically(
    resources: RunFindingResources,
) -> None:
    with pool.connection() as connection:
        approved_finding_id = create_finding(
            connection,
            resources.origin_run_id,
            status=FindingStatus.APPROVED,
        )
        rejected_finding_id = create_finding(
            connection,
            resources.origin_run_id,
            status=FindingStatus.REJECTED,
        )
        repository = RunFindingRepository(connection)
        repository.add(
            resources.carried_run_id,
            resources.finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        repository.add(
            resources.carried_run_id,
            approved_finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        repository.add(
            resources.carried_run_id,
            rejected_finding_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

    with pool.connection() as connection:
        repository = RunFindingRepository(connection)
        records = repository.list_review_state_for_run(resources.carried_run_id)
        known_empty = repository.list_review_state_for_run(resources.empty_run_id)
        unknown = repository.list_review_state_for_run(str(uuid4()))

    assert [record.finding_id for record in records] == sorted(
        [resources.finding_id, approved_finding_id, rejected_finding_id],
    )
    assert {record.status for record in records} == {
        FindingStatus.PENDING,
        FindingStatus.APPROVED,
        FindingStatus.REJECTED,
    }
    assert all(record.relation is RunMembershipRelation.CARRIED_FORWARD for record in records)
    assert all(record.origin_run_id == resources.origin_run_id for record in records)
    assert known_empty == []
    assert unknown == []
