from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, RaiseException

from db.pool import pool
from domain.finding import Finding, FindingStatus, FindingType
from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot
from repositories.finding_repository import FindingRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository


@dataclass(frozen=True)
class FindingResources:
    pile_id: str
    origin_run_id: str
    rule_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> FindingResources:
    with pool.connection() as connection:
        pile_row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Finding repository test {uuid4()}",),
        ).fetchone()
        assert pile_row is not None
        pile_id = str(pile_row[0])
        origin_run_id = create_manual_run(connection, pile_id)
        rule_id = create_rule(connection, pile_id)
        rule_set_row = connection.execute(
            """
            SELECT rule_sets.id, rule_sets.name, rule_sets.description, rule_sets.version,
                   rules.rule_key, rules.title, rules.description, rules.severity, rules.definition
            FROM rules
            JOIN rule_sets ON rule_sets.id = rules.rule_set_id
            WHERE rules.id = %s::uuid
            """,
            (rule_id,),
        ).fetchone()
        assert rule_set_row is not None
        scope_repository = RunRuleScopeRepository(connection)
        scope_repository.insert_rule_set_snapshot(
            RunRuleSetSnapshot(
                run_id=origin_run_id,
                source_rule_set_id=str(rule_set_row[0]),
                name=str(rule_set_row[1]),
                description=rule_set_row[2],
                version=int(rule_set_row[3]),
            ),
        )
        scope_repository.insert_rule_snapshot(
            RunRuleSnapshot(
                run_id=origin_run_id,
                source_rule_id=rule_id,
                source_rule_set_id=str(rule_set_row[0]),
                rule_key=str(rule_set_row[4]),
                title=str(rule_set_row[5]),
                description=rule_set_row[6],
                severity=str(rule_set_row[7]),
                definition=rule_set_row[8],
            ),
        )
        scope_repository.complete_scope(origin_run_id)
        connection.commit()

    created_resources = FindingResources(pile_id, origin_run_id, rule_id)

    yield created_resources


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


def create_rule(connection, pile_id: str) -> str:
    rule_set_row = connection.execute(
        """
        INSERT INTO rule_sets (pile_id, name, version)
        VALUES (%s::uuid, %s, 1)
        RETURNING id
        """,
        (pile_id, f"Rule set {uuid4()}"),
    ).fetchone()
    assert rule_set_row is not None
    rule_row = connection.execute(
        """
        INSERT INTO rules (rule_set_id, rule_key, title, severity, definition)
        VALUES (%s::uuid, %s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (str(rule_set_row[0]), f"rule-{uuid4()}", "Required clause", "HIGH", "{}"),
    ).fetchone()
    assert rule_row is not None
    return str(rule_row[0])


def make_finding(
    resources: FindingResources,
    *,
    finding_type: FindingType = FindingType.RULE_VIOLATION,
    rule_id: str | None = None,
    status: FindingStatus = FindingStatus.PENDING,
    description: str | None = "The required clause is missing.",
    origin_run_id: str | None = None,
) -> Finding:
    return Finding(
        finding_id=str(uuid4()),
        origin_run_id=origin_run_id or resources.origin_run_id,
        finding_type=finding_type,
        rule_id=resources.rule_id if rule_id is None and finding_type is FindingType.RULE_VIOLATION else rule_id,
        title="Required clause is missing",
        description=description,
        severity="HIGH",
        status=status,
    )


def test_create_get_list_and_to_domain_round_trip(resources: FindingResources) -> None:
    first_finding = make_finding(resources)
    second_finding = make_finding(
        resources,
        finding_type=FindingType.CONTRADICTION,
        rule_id=None,
        description=None,
    )

    with pool.connection() as connection:
        repository = FindingRepository(connection)
        first = repository.create(first_finding)
        second = repository.create(second_finding)
        connection.commit()

    with pool.connection() as connection:
        repository = FindingRepository(connection)
        retrieved = repository.get_by_id(first.finding_id)
        listed = repository.list_for_origin_run(resources.origin_run_id)
        missing = repository.get_by_id(str(uuid4()))

    assert first.created_at is not None
    assert first.updated_at is not None
    assert first.to_domain() == first_finding
    assert second.to_domain() == second_finding
    assert retrieved == first
    assert listed == sorted([first, second], key=lambda record: (record.created_at, record.finding_id))
    assert missing is None


def test_create_persists_the_supplied_status(resources: FindingResources) -> None:
    finding = make_finding(resources, status=FindingStatus.APPROVED)

    with pool.connection() as connection:
        record = FindingRepository(connection).create(finding)
        connection.commit()

    assert record.status is FindingStatus.APPROVED


def test_database_rejects_invalid_rule_type_relationships(resources: FindingResources) -> None:
    with pool.connection() as connection:
        for finding_type, rule_id in [
            ("RULE_VIOLATION", None),
            ("CONTRADICTION", resources.rule_id),
        ]:
            with pytest.raises(CheckViolation):
                connection.execute(
                    """
                    INSERT INTO findings (
                        origin_run_id, rule_id, finding_type, status, title, severity
                    )
                    VALUES (%s::uuid, %s::uuid, %s::finding_type, 'PENDING'::finding_status, %s, %s)
                    """,
                    (resources.origin_run_id, rule_id, finding_type, "invalid", "HIGH"),
                )

            connection.rollback()


def test_database_rejects_unknown_origin_run_and_rule(resources: FindingResources) -> None:
    with pool.connection() as connection:
        repository = FindingRepository(connection)

        with pytest.raises(ForeignKeyViolation):
            repository.create(make_finding(resources, origin_run_id=str(uuid4())))

        connection.rollback()

        with pytest.raises(ForeignKeyViolation):
            repository.create(make_finding(resources, rule_id=str(uuid4())))

        connection.rollback()


def test_database_rejects_existing_rule_without_origin_scope_snapshot(
    resources: FindingResources,
) -> None:
    with pool.connection() as connection:
        uncaptured_run_id = create_manual_run(connection, resources.pile_id)
        uncaptured_rule_id = create_rule(connection, resources.pile_id)
        connection.commit()

        with pytest.raises(RaiseException, match="requires an origin-run rule snapshot"):
            FindingRepository(connection).create(
                Finding(
                    finding_id=str(uuid4()),
                    origin_run_id=uncaptured_run_id,
                    finding_type=FindingType.RULE_VIOLATION,
                    rule_id=uncaptured_rule_id,
                    title="Uncaptured rule violation",
                    severity="HIGH",
                ),
            )

        connection.rollback()


@pytest.mark.parametrize("status", [FindingStatus.APPROVED, FindingStatus.REJECTED])
def test_pending_status_updates_commit_and_advance_updated_at(
    resources: FindingResources,
    status: FindingStatus,
) -> None:
    finding = make_finding(resources)

    with pool.connection() as connection:
        created = FindingRepository(connection).create(finding)
        connection.commit()

    with pool.connection() as connection:
        updated = FindingRepository(connection).update_status(finding.finding_id, status)
        connection.commit()

    assert updated is not None
    assert updated.status is status
    assert updated.updated_at > created.updated_at


@pytest.mark.parametrize(
    ("initial", "requested"),
    [
        (FindingStatus.APPROVED, FindingStatus.REJECTED),
        (FindingStatus.APPROVED, FindingStatus.PENDING),
        (FindingStatus.REJECTED, FindingStatus.APPROVED),
        (FindingStatus.REJECTED, FindingStatus.PENDING),
    ],
)
def test_database_rejects_terminal_status_changes(
    resources: FindingResources,
    initial: FindingStatus,
    requested: FindingStatus,
) -> None:
    finding = make_finding(resources, status=initial)

    with pool.connection() as connection:
        FindingRepository(connection).create(finding)
        connection.commit()

    with pool.connection() as connection:
        with pytest.raises(RaiseException):
            FindingRepository(connection).update_status(finding.finding_id, requested)

        connection.rollback()


@pytest.mark.parametrize(
    "status",
    [FindingStatus.PENDING, FindingStatus.APPROVED, FindingStatus.REJECTED],
)
def test_same_status_update_is_permitted_and_advances_timestamp(
    resources: FindingResources,
    status: FindingStatus,
) -> None:
    finding = make_finding(resources, status=status)

    with pool.connection() as connection:
        created = FindingRepository(connection).create(finding)
        connection.commit()

    with pool.connection() as connection:
        updated = FindingRepository(connection).update_status(finding.finding_id, status)
        connection.commit()

    assert updated is not None
    assert updated.status is status
    assert updated.updated_at > created.updated_at


def test_database_rejects_historical_finding_content_mutation(resources: FindingResources) -> None:
    finding = make_finding(resources)

    with pool.connection() as connection:
        FindingRepository(connection).create(finding)
        connection.commit()

        with pytest.raises(RaiseException):
            connection.execute(
                "UPDATE findings SET title = %s WHERE id = %s::uuid",
                ("rewritten", finding.finding_id),
            )

        connection.rollback()


def test_update_status_returns_none_for_unknown_finding(resources: FindingResources) -> None:
    with pool.connection() as connection:
        record = FindingRepository(connection).update_status(str(uuid4()), FindingStatus.APPROVED)

    assert record is None


def test_caller_rollback_removes_uncommitted_finding(resources: FindingResources) -> None:
    finding = make_finding(resources)

    with pool.connection() as connection:
        FindingRepository(connection).create(finding)
        connection.rollback()

    with pool.connection() as connection:
        record = FindingRepository(connection).get_by_id(finding.finding_id)

    assert record is None
