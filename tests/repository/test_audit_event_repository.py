from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation

from db.pool import pool
from repositories.audit_event_repository import AuditEventRepository


@dataclass(frozen=True)
class AuditEventResources:
    pile_id: str
    run_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> AuditEventResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Audit event repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        run_id = create_manual_run(connection, pile_id)
        connection.commit()

    created_resources = AuditEventResources(pile_id, run_id)

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM audit_events WHERE run_id = %s::uuid",
                (created_resources.run_id,),
            )
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


@pytest.mark.parametrize(
    "payload",
    [
        None,
        True,
        "completed",
        7,
        ["one", {"two": False}],
        {"review": {"approved": True}, "outputs": ["report", "summary"]},
    ],
)
def test_create_get_and_jsonb_round_trip(resources: AuditEventResources, payload: object) -> None:
    entity_id = str(uuid4())

    with pool.connection() as connection:
        record = AuditEventRepository(connection).create(
            resources.run_id,
            "run",
            entity_id,
            "state_recorded",
            "system",
            "worker-1",
            payload,
        )
        connection.commit()

    with pool.connection() as connection:
        retrieved = AuditEventRepository(connection).get_by_id(record.audit_event_id)

    assert record.audit_event_id
    assert record.created_at is not None
    assert record.run_id == resources.run_id
    assert record.entity_type == "run"
    assert record.entity_id == entity_id
    assert record.event_type == "state_recorded"
    assert record.actor_type == "system"
    assert record.actor_id == "worker-1"
    assert record.payload == payload
    assert retrieved == record


def test_list_for_run_is_deterministic(resources: AuditEventResources) -> None:
    with pool.connection() as connection:
        repository = AuditEventRepository(connection)
        first = repository.create(
            resources.run_id,
            "finding",
            str(uuid4()),
            "first_event",
            "human",
            "reviewer-1",
            {"ordinal": 1},
        )
        second = repository.create(
            resources.run_id,
            "finding",
            str(uuid4()),
            "second_event",
            "human",
            "reviewer-2",
            {"ordinal": 2},
        )
        connection.commit()

    with pool.connection() as connection:
        listed = AuditEventRepository(connection).list_for_run(resources.run_id)

    assert listed == sorted(
        [first, second],
        key=lambda record: (record.created_at, record.audit_event_id),
    )


def test_unknown_audit_event_returns_none(resources: AuditEventResources) -> None:
    with pool.connection() as connection:
        record = AuditEventRepository(connection).get_by_id(str(uuid4()))

    assert record is None


def test_entity_id_without_a_foreign_key_is_persisted(resources: AuditEventResources) -> None:
    entity_id = str(uuid4())

    with pool.connection() as connection:
        record = AuditEventRepository(connection).create(
            resources.run_id,
            "external_entity",
            entity_id,
            "external_event",
            "service",
            "service-1",
            {},
        )
        connection.commit()

    assert record.entity_id == entity_id


def test_unknown_run_is_rejected_by_foreign_key(resources: AuditEventResources) -> None:
    with pool.connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            AuditEventRepository(connection).create(
                str(uuid4()),
                "run",
                str(uuid4()),
                "state_recorded",
                "system",
                "worker-1",
                {},
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_audit_event(resources: AuditEventResources) -> None:
    with pool.connection() as connection:
        record = AuditEventRepository(connection).create(
            resources.run_id,
            "run",
            str(uuid4()),
            "state_recorded",
            "system",
            "worker-1",
            {"pending": True},
        )
        connection.rollback()

    with pool.connection() as connection:
        persisted = AuditEventRepository(connection).get_by_id(record.audit_event_id)

    assert persisted is None
