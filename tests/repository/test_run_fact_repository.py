from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.fact import Fact
from domain.run_membership import RunMembershipRelation
from repositories.fact_repository import FactRepository
from repositories.run_fact_repository import RunFactRepository


@dataclass(frozen=True)
class RunFactResources:
    pile_id: str
    origin_run_id: str
    carried_run_id: str
    fact_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> RunFactResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Run fact repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        origin_run_id = create_manual_run(connection, pile_id)
        carried_run_id = create_manual_run(connection, pile_id)
        fact_id = create_fact(connection, origin_run_id)
        connection.commit()

    created_resources = RunFactResources(pile_id, origin_run_id, carried_run_id, fact_id)

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM fact_evidence WHERE fact_id = %s::uuid",
                (created_resources.fact_id,),
            )
            connection.execute(
                "DELETE FROM run_facts WHERE run_id IN (%s::uuid, %s::uuid)",
                (created_resources.origin_run_id, created_resources.carried_run_id),
            )
            connection.execute(
                "DELETE FROM facts WHERE origin_run_id = %s::uuid",
                (created_resources.origin_run_id,),
            )
            connection.execute(
                "DELETE FROM runs WHERE id IN (%s::uuid, %s::uuid)",
                (created_resources.origin_run_id, created_resources.carried_run_id),
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


def create_fact(connection, origin_run_id: str) -> str:
    fact = Fact(
        fact_id=str(uuid4()),
        origin_run_id=origin_run_id,
        subject="loan",
        predicate="has_amount",
        object_value=100000,
    )
    return FactRepository(connection).create(fact).fact_id


def test_created_and_carried_forward_memberships_preserve_fact_origin(
    resources: RunFactResources,
) -> None:
    with pool.connection() as connection:
        repository = RunFactRepository(connection)
        created = repository.add(
            resources.origin_run_id,
            resources.fact_id,
            RunMembershipRelation.CREATED,
        )
        carried = repository.add(
            resources.carried_run_id,
            resources.fact_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

    with pool.connection() as connection:
        repository = RunFactRepository(connection)
        retrieved = repository.get(resources.origin_run_id, resources.fact_id)
        listed = repository.list_for_run(resources.carried_run_id)
        fact = FactRepository(connection).get_by_id(resources.fact_id)

    assert created.relation is RunMembershipRelation.CREATED
    assert carried.relation is RunMembershipRelation.CARRIED_FORWARD
    assert retrieved == created
    assert listed == [carried]
    assert fact is not None
    assert fact.origin_run_id == resources.origin_run_id


def test_list_for_run_is_deterministic_and_unknown_membership_is_none(
    resources: RunFactResources,
) -> None:
    with pool.connection() as connection:
        second_fact_id = create_fact(connection, resources.origin_run_id)
        repository = RunFactRepository(connection)
        first = repository.add(
            resources.carried_run_id,
            resources.fact_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        second = repository.add(
            resources.carried_run_id,
            second_fact_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        connection.commit()

    with pool.connection() as connection:
        repository = RunFactRepository(connection)
        listed = repository.list_for_run(resources.carried_run_id)
        missing = repository.get(resources.origin_run_id, second_fact_id)

    assert listed == sorted([first, second], key=lambda record: (record.created_at, record.fact_id))
    assert missing is None


def test_duplicate_membership_is_rejected(resources: RunFactResources) -> None:
    with pool.connection() as connection:
        repository = RunFactRepository(connection)
        repository.add(
            resources.origin_run_id,
            resources.fact_id,
            RunMembershipRelation.CREATED,
        )
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.add(
                resources.origin_run_id,
                resources.fact_id,
                RunMembershipRelation.CREATED,
            )

        connection.rollback()


def test_unknown_foreign_keys_are_rejected(resources: RunFactResources) -> None:
    with pool.connection() as connection:
        repository = RunFactRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.add(
                str(uuid4()),
                resources.fact_id,
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


def test_caller_rollback_removes_uncommitted_membership(resources: RunFactResources) -> None:
    with pool.connection() as connection:
        RunFactRepository(connection).add(
            resources.origin_run_id,
            resources.fact_id,
            RunMembershipRelation.CREATED,
        )
        connection.rollback()

    with pool.connection() as connection:
        record = RunFactRepository(connection).get(resources.origin_run_id, resources.fact_id)

    assert record is None
