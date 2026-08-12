from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, RaiseException

from db.pool import pool
from domain.fact import Fact, FactStatus
from repositories.fact_repository import FactRepository


@dataclass(frozen=True)
class FactResources:
    pile_id: str
    origin_run_id: str
    other_run_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> FactResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            """
            INSERT INTO piles (name)
            VALUES (%s)
            RETURNING id
            """,
            (f"Fact repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        origin_run_id = create_manual_run(connection, pile_id)
        other_run_id = create_manual_run(connection, pile_id)
        connection.commit()

    created_resources = FactResources(pile_id, origin_run_id, other_run_id)

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM fact_evidence
                WHERE fact_id IN (
                    SELECT id
                    FROM facts
                    WHERE origin_run_id IN (%s::uuid, %s::uuid)
                )
                """,
                (created_resources.origin_run_id, created_resources.other_run_id),
            )
            connection.execute(
                "DELETE FROM run_facts WHERE run_id IN (%s::uuid, %s::uuid)",
                (created_resources.origin_run_id, created_resources.other_run_id),
            )
            connection.execute(
                "DELETE FROM facts WHERE origin_run_id IN (%s::uuid, %s::uuid)",
                (created_resources.origin_run_id, created_resources.other_run_id),
            )
            connection.execute(
                "DELETE FROM runs WHERE id IN (%s::uuid, %s::uuid)",
                (created_resources.origin_run_id, created_resources.other_run_id),
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


def make_fact(
    resources: FactResources,
    object_value: object = "100000",
    *,
    status: FactStatus = FactStatus.CONFIRMED,
    origin_run_id: str | None = None,
) -> Fact:
    return Fact(
        fact_id=str(uuid4()),
        origin_run_id=origin_run_id or resources.origin_run_id,
        subject="loan",
        predicate="has_amount",
        object_value=object_value,
        status=status,
    )


def test_create_get_list_and_to_domain_round_trip(resources: FactResources) -> None:
    first_fact = make_fact(resources, {"amount": 100000})
    second_fact = make_fact(resources, {"currency": "USD"}, status=FactStatus.DISPUTED)

    with pool.connection() as connection:
        repository = FactRepository(connection)
        first = repository.create(first_fact)
        second = repository.create(second_fact)
        connection.commit()

    with pool.connection() as connection:
        repository = FactRepository(connection)
        retrieved = repository.get_by_id(first.fact_id)
        listed = repository.list_for_origin_run(resources.origin_run_id)
        missing = repository.get_by_id(str(uuid4()))

    assert first.created_at is not None
    assert first.to_domain().model_dump(mode="json") == first_fact.model_dump(mode="json")
    assert retrieved == first
    assert listed == sorted([first, second], key=lambda record: (record.created_at, record.fact_id))
    assert missing is None


@pytest.mark.parametrize(
    "object_value",
    [
        "text",
        7,
        3.5,
        True,
        None,
        ["value", 1, {"enabled": True}],
        {"nested": {"items": ["value", {"amount": 100000}]}},
    ],
)
def test_jsonb_round_trip_preserves_json_compatible_values(
    resources: FactResources,
    object_value: object,
) -> None:
    fact = make_fact(resources, object_value)

    with pool.connection() as connection:
        created = FactRepository(connection).create(fact)
        connection.commit()

    with pool.connection() as connection:
        retrieved = FactRepository(connection).get_by_id(fact.fact_id)

    assert created.to_domain().model_dump(mode="json")["object_value"] == object_value
    assert retrieved is not None
    assert retrieved.to_domain().model_dump(mode="json")["object_value"] == object_value


def test_jsonb_readback_reconstructs_deeply_immutable_fact(resources: FactResources) -> None:
    fact = make_fact(resources, {"nested": {"items": [{"amount": 100000}]}})

    with pool.connection() as connection:
        FactRepository(connection).create(fact)
        connection.commit()

    with pool.connection() as connection:
        record = FactRepository(connection).get_by_id(fact.fact_id)

    assert record is not None
    restored = record.to_domain()

    with pytest.raises(TypeError):
        restored.object_value["nested"] = "changed"

    with pytest.raises(AttributeError):
        restored.object_value["nested"]["items"].append("changed")

    with pytest.raises(TypeError):
        restored.object_value["nested"]["items"][0]["amount"] = 1


def test_superceded_is_valid_when_creating_a_new_fact(resources: FactResources) -> None:
    fact = make_fact(resources, status=FactStatus.SUPERSEDED)

    with pool.connection() as connection:
        record = FactRepository(connection).create(fact)
        connection.commit()

    assert record.status is FactStatus.SUPERSEDED


def test_unknown_origin_run_is_rejected_by_foreign_key(resources: FactResources) -> None:
    with pool.connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            FactRepository(connection).create(
                make_fact(resources, origin_run_id=str(uuid4())),
            )

        connection.rollback()


def test_database_rejects_historical_fact_status_mutation(resources: FactResources) -> None:
    fact = make_fact(resources, status=FactStatus.CONFIRMED)

    with pool.connection() as connection:
        FactRepository(connection).create(fact)
        connection.commit()

        with pytest.raises(RaiseException):
            connection.execute(
                "UPDATE facts SET status = 'SUPERSEDED'::fact_status WHERE id = %s::uuid",
                (fact.fact_id,),
            )

        connection.rollback()

    with pool.connection() as connection:
        record = FactRepository(connection).get_by_id(fact.fact_id)

    assert record is not None
    assert record.status is FactStatus.CONFIRMED


def test_caller_rollback_removes_uncommitted_fact(resources: FactResources) -> None:
    fact = make_fact(resources)

    with pool.connection() as connection:
        FactRepository(connection).create(fact)
        connection.rollback()

    with pool.connection() as connection:
        record = FactRepository(connection).get_by_id(fact.fact_id)

    assert record is None
