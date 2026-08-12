from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from repositories.output_repository import OutputRepository


@dataclass(frozen=True)
class OutputResources:
    pile_id: str
    run_id: str
    second_run_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> OutputResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Output repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        run_id = create_manual_run(connection, pile_id)
        second_run_id = create_manual_run(connection, pile_id)
        connection.commit()

    created_resources = OutputResources(pile_id, run_id, second_run_id)

    try:
        yield created_resources
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM outputs WHERE run_id IN (%s::uuid, %s::uuid)",
                (created_resources.run_id, created_resources.second_run_id),
            )
            connection.execute(
                "DELETE FROM runs WHERE id IN (%s::uuid, %s::uuid)",
                (created_resources.run_id, created_resources.second_run_id),
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


def test_create_get_and_list_outputs(resources: OutputResources) -> None:
    with pool.connection() as connection:
        repository = OutputRepository(connection)
        first = repository.create(
            resources.run_id,
            "REPORT",
            "file:///artifacts/report.json",
            "sha256:report",
        )
        second = repository.create(
            resources.run_id,
            "SUMMARY",
            "file:///artifacts/summary.json",
            "sha256:summary",
        )
        connection.commit()

    with pool.connection() as connection:
        repository = OutputRepository(connection)
        retrieved = repository.get_by_id(first.output_id)
        by_type = repository.get_by_run_and_type(resources.run_id, "REPORT")
        listed = repository.list_for_run(resources.run_id)

    assert first.output_id
    assert first.created_at is not None
    assert first.run_id == resources.run_id
    assert first.output_type == "REPORT"
    assert first.storage_uri == "file:///artifacts/report.json"
    assert first.content_hash == "sha256:report"
    assert retrieved == first
    assert by_type == first
    assert listed == sorted([first, second], key=lambda record: (record.created_at, record.output_id))


def test_unknown_output_lookups_return_none(resources: OutputResources) -> None:
    with pool.connection() as connection:
        repository = OutputRepository(connection)
        by_id = repository.get_by_id(str(uuid4()))
        by_type = repository.get_by_run_and_type(resources.run_id, f"unknown-{uuid4()}")

    assert by_id is None
    assert by_type is None


def test_same_output_type_is_allowed_for_different_runs(resources: OutputResources) -> None:
    with pool.connection() as connection:
        repository = OutputRepository(connection)
        first = repository.create(resources.run_id, "REPORT", "file:///first", "sha256:first")
        second = repository.create(resources.second_run_id, "REPORT", "file:///second", "sha256:second")
        connection.commit()

    assert first.output_type == second.output_type
    assert first.run_id != second.run_id


def test_duplicate_run_output_type_is_rejected(resources: OutputResources) -> None:
    with pool.connection() as connection:
        repository = OutputRepository(connection)
        repository.create(resources.run_id, "REPORT", "file:///first", "sha256:first")
        connection.commit()

        with pytest.raises(UniqueViolation):
            repository.create(resources.run_id, "REPORT", "file:///second", "sha256:second")

        connection.rollback()


def test_unknown_run_is_rejected_by_foreign_key(resources: OutputResources) -> None:
    with pool.connection() as connection:
        with pytest.raises(ForeignKeyViolation):
            OutputRepository(connection).create(
                str(uuid4()),
                "REPORT",
                "file:///unknown",
                "sha256:unknown",
            )

        connection.rollback()


def test_caller_rollback_removes_uncommitted_output(resources: OutputResources) -> None:
    with pool.connection() as connection:
        record = OutputRepository(connection).create(
            resources.run_id,
            "REPORT",
            "file:///rolled-back",
            "sha256:rolled-back",
        )
        connection.rollback()

    with pool.connection() as connection:
        persisted = OutputRepository(connection).get_by_id(record.output_id)

    assert persisted is None
