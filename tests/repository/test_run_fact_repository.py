from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from db.pool import pool
from domain.fact import Fact, FactStatus
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


def create_fact(
    connection,
    origin_run_id: str,
    *,
    object_value: object = 100000,
    status: FactStatus = FactStatus.CONFIRMED,
) -> str:
    fact = Fact(
        fact_id=str(uuid4()),
        origin_run_id=origin_run_id,
        subject="loan",
        predicate="has_amount",
        object_value=object_value,
        status=status,
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


def test_reconciliation_input_is_joined_and_deterministically_ordered(
    resources: RunFactResources,
) -> None:
    with pool.connection() as connection:
        document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (resources.pile_id, f"reconciliation-{uuid4()}"),
        ).fetchone()
        assert document is not None
        version = connection.execute(
            """
            INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri)
            VALUES (%s::uuid, 1, %s, %s)
            RETURNING id
            """,
            (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        assert version is not None
        chunk = connection.execute(
            """
            INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
            VALUES (%s::uuid, 0, '100 / 110', %s)
            RETURNING id
            """,
            (str(version[0]), f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert chunk is not None
        evidence = connection.execute(
            """
            INSERT INTO evidence_spans (
                document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash
            )
            VALUES (%s::uuid, %s::uuid, 0, 3, '100', %s)
            RETURNING id
            """,
            (str(version[0]), str(chunk[0]), f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert evidence is not None
        second_evidence = connection.execute(
            """
            INSERT INTO evidence_spans (
                document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash
            )
            VALUES (%s::uuid, %s::uuid, 6, 9, '110', %s)
            RETURNING id
            """,
            (str(version[0]), str(chunk[0]), f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert second_evidence is not None
        confirmed = create_fact(connection, resources.origin_run_id, object_value={"amount": 100})
        disputed = create_fact(
            connection,
            resources.origin_run_id,
            object_value={"amount": 110},
            status=FactStatus.DISPUTED,
        )
        superseded = create_fact(
            connection,
            resources.origin_run_id,
            object_value={"amount": 90},
            status=FactStatus.SUPERSEDED,
        )
        repository = RunFactRepository(connection)
        repository.add(resources.carried_run_id, confirmed, RunMembershipRelation.CARRIED_FORWARD)
        repository.add(resources.carried_run_id, disputed, RunMembershipRelation.CARRIED_FORWARD)
        repository.add(resources.carried_run_id, superseded, RunMembershipRelation.CARRIED_FORWARD)
        connection.execute(
            "INSERT INTO fact_evidence (fact_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (confirmed, str(evidence[0])),
        )
        connection.execute(
            "INSERT INTO fact_evidence (fact_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (confirmed, str(second_evidence[0])),
        )
        connection.execute(
            "INSERT INTO fact_evidence (fact_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (disputed, str(second_evidence[0])),
        )
        connection.commit()

    with pool.connection() as connection:
        records = RunFactRepository(connection).list_reconciliation_input_for_run(
            resources.carried_run_id,
        )
        missing = RunFactRepository(connection).list_reconciliation_input_for_run(str(uuid4()))

    assert [record.fact_id for record in records] == sorted([confirmed, disputed, superseded])
    by_id = {record.fact_id: record for record in records}
    assert by_id[confirmed].relation is RunMembershipRelation.CARRIED_FORWARD
    assert by_id[confirmed].object_value == {"amount": 100}
    assert [item.evidence_id for item in by_id[confirmed].evidence] == sorted(
        [str(evidence[0]), str(second_evidence[0])],
    )
    assert by_id[disputed].status is FactStatus.DISPUTED
    assert by_id[superseded].evidence == ()
    assert missing == []

    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM fact_evidence WHERE fact_id IN (%s::uuid, %s::uuid, %s::uuid)",
            (confirmed, disputed, superseded),
        )
        connection.execute(
            "DELETE FROM run_facts WHERE fact_id IN (%s::uuid, %s::uuid, %s::uuid)",
            (confirmed, disputed, superseded),
        )
        connection.execute(
            "DELETE FROM facts WHERE id IN (%s::uuid, %s::uuid, %s::uuid)",
            (confirmed, disputed, superseded),
        )
        connection.execute(
            "DELETE FROM evidence_spans WHERE id IN (%s::uuid, %s::uuid)",
            (str(evidence[0]), str(second_evidence[0])),
        )
        connection.execute("DELETE FROM document_chunks WHERE id = %s::uuid", (str(chunk[0]),))
        connection.execute("DELETE FROM document_versions WHERE id = %s::uuid", (str(version[0]),))
        connection.execute("DELETE FROM documents WHERE id = %s::uuid", (str(document[0]),))
        connection.commit()
