from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import ForeignKeyViolation, LockNotAvailable, RaiseException, UniqueViolation
from psycopg.types.json import Jsonb

from db.pool import pool
from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot
from repositories.run_rule_scope_repository import RunRuleScopeRepository


@dataclass(frozen=True)
class RuleScopeResources:
    pile_id: str
    run_id: str
    other_pile_id: str
    rule_set_id: str
    other_rule_set_id: str
    first_rule_id: str
    second_rule_id: str
    other_rule_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def create_resources() -> RuleScopeResources:
    with pool.connection() as connection:
        pile_id = create_pile(connection, "Run rule scope")
        other_pile_id = create_pile(connection, "Other run rule scope")
        run_id = create_run(connection, pile_id)
        rule_set_id = create_rule_set(connection, pile_id, "Contract rules", "Primary rules", 1)
        other_rule_set_id = create_rule_set(
            connection,
            other_pile_id,
            "Other rules",
            "Other pile rules",
            1,
        )
        first_rule_id = create_rule(
            connection,
            rule_set_id,
            "required-clause",
            "Required clause",
            "The clause must be present.",
            "HIGH",
            {"all": ["governing_law", {"required": True}]},
        )
        second_rule_id = create_rule(
            connection,
            rule_set_id,
            "notice-period",
            "Notice period",
            None,
            "MEDIUM",
            {"minimum_days": 30},
        )
        other_rule_id = create_rule(
            connection,
            other_rule_set_id,
            "other-rule",
            "Other rule",
            None,
            "LOW",
            {"outside": True},
        )
        connection.commit()

    return RuleScopeResources(
        pile_id,
        run_id,
        other_pile_id,
        rule_set_id,
        other_rule_set_id,
        first_rule_id,
        second_rule_id,
        other_rule_id,
    )


def create_pile(connection, label: str) -> str:
    row = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"{label} {uuid4()}",),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_run(connection, pile_id: str) -> str:
    run_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO runs (id, pile_id, run_type, status)
        VALUES (%s::uuid, %s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
        """,
        (run_id, pile_id),
    )
    return run_id


def create_rule_set(
    connection,
    pile_id: str,
    name: str,
    description: str | None,
    version: int,
) -> str:
    row = connection.execute(
        """
        INSERT INTO rule_sets (pile_id, name, description, version)
        VALUES (%s::uuid, %s, %s, %s)
        RETURNING id
        """,
        (pile_id, name, description, version),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_rule(
    connection,
    rule_set_id: str,
    rule_key: str,
    title: str,
    description: str | None,
    severity: str,
    definition: object,
) -> str:
    row = connection.execute(
        """
        INSERT INTO rules (rule_set_id, rule_key, title, description, severity, definition)
        VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (rule_set_id, rule_key, title, description, severity, Jsonb(definition)),
    ).fetchone()
    assert row is not None
    return str(row[0])


def rule_set_snapshot(resources: RuleScopeResources) -> RunRuleSetSnapshot:
    return RunRuleSetSnapshot(
        run_id=resources.run_id,
        source_rule_set_id=resources.rule_set_id,
        name="Contract rules",
        description="Primary rules",
        version=1,
    )


def rule_snapshot(resources: RuleScopeResources, rule_id: str, *, second: bool = False) -> RunRuleSnapshot:
    if second:
        return RunRuleSnapshot(
            run_id=resources.run_id,
            source_rule_id=rule_id,
            source_rule_set_id=resources.rule_set_id,
            rule_key="notice-period",
            title="Notice period",
            description=None,
            severity="MEDIUM",
            definition={"minimum_days": 30},
        )

    return RunRuleSnapshot(
        run_id=resources.run_id,
        source_rule_id=rule_id,
        source_rule_set_id=resources.rule_set_id,
        rule_key="required-clause",
        title="Required clause",
        description="The clause must be present.",
        severity="HIGH",
        definition={"all": ["governing_law", {"required": True}]},
    )


def close_scope(resources: RuleScopeResources, *, include_rules: bool = True) -> None:
    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        repository.insert_rule_set_snapshot(rule_set_snapshot(resources))
        if include_rules:
            repository.insert_rule_snapshot(rule_snapshot(resources, resources.first_rule_id))
        repository.complete_scope(resources.run_id)
        connection.commit()


def test_header_last_scope_round_trip_and_deterministic_listing() -> None:
    resources = create_resources()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        rule_set = repository.insert_rule_set_snapshot(rule_set_snapshot(resources))
        second = repository.insert_rule_snapshot(
            rule_snapshot(resources, resources.second_rule_id, second=True),
        )
        first = repository.insert_rule_snapshot(rule_snapshot(resources, resources.first_rule_id))
        scope = repository.complete_scope(resources.run_id)
        connection.commit()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        stored_scope = repository.get_scope(resources.run_id)
        rule_sets = repository.list_rule_sets_for_run(resources.run_id)
        rules = repository.list_rules_for_run(resources.run_id)

    assert scope.captured_at is not None
    assert scope.created_at is not None
    assert stored_scope == scope
    assert rule_set.to_domain() == rule_set_snapshot(resources)
    assert [record.source_rule_id for record in rules] == sorted(
        [first.source_rule_id, second.source_rule_id],
    )
    assert rules[0].to_domain().definition in ({"minimum_days": 30}, {"all": ["governing_law", {"required": True}]})
    assert rule_sets == [rule_set]


def test_committed_empty_scope_is_distinct_from_uncaptured_scope() -> None:
    resources = create_resources()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        repository.complete_scope(resources.run_id)
        connection.commit()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        empty_scope = repository.get_scope(resources.run_id)
        empty_rule_sets = repository.list_rule_sets_for_run(resources.run_id)
        empty_rules = repository.list_rules_for_run(resources.run_id)
        unknown_scope = repository.get_scope(str(uuid4()))

    assert empty_scope is not None
    assert empty_rule_sets == []
    assert empty_rules == []
    assert unknown_scope is None


def test_selected_empty_rule_set_is_preserved_in_a_closed_scope() -> None:
    resources = create_resources()
    close_scope(resources, include_rules=False)

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        rule_sets = repository.list_rule_sets_for_run(resources.run_id)
        rules = repository.list_rules_for_run(resources.run_id)

    assert [record.source_rule_set_id for record in rule_sets] == [resources.rule_set_id]
    assert rules == []


def test_deferred_header_fk_rejects_children_without_completion_header() -> None:
    resources = create_resources()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        repository.insert_rule_set_snapshot(rule_set_snapshot(resources))

        with pytest.raises(ForeignKeyViolation):
            connection.commit()

        connection.rollback()


def test_closed_scope_rejects_later_child_inserts_and_direct_mutation() -> None:
    resources = create_resources()
    close_scope(resources)

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        with pytest.raises(RaiseException, match="already closed"):
            repository.insert_rule_snapshot(
                rule_snapshot(resources, resources.second_rule_id, second=True),
            )
        connection.rollback()

        with pytest.raises(RaiseException, match="immutable"):
            connection.execute(
                "UPDATE run_rule_snapshots SET title = 'rewritten' WHERE run_id = %s::uuid",
                (resources.run_id,),
            )
        connection.rollback()

        with pytest.raises(RaiseException, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM run_rule_snapshots WHERE run_id = %s::uuid",
                (resources.run_id,),
            )
        connection.rollback()

        with pytest.raises(RaiseException, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM run_rule_set_snapshots WHERE run_id = %s::uuid",
                (resources.run_id,),
            )
        connection.rollback()

        with pytest.raises(RaiseException, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM run_rule_scopes WHERE run_id = %s::uuid",
                (resources.run_id,),
            )
        connection.rollback()

        with pytest.raises(ForeignKeyViolation):
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (resources.run_id,))
        connection.rollback()


def test_snapshot_copies_json_content_and_ignores_later_source_mutation() -> None:
    resources = create_resources()
    close_scope(resources)

    with pool.connection() as connection:
        connection.execute(
            "UPDATE rules SET definition = %s::jsonb, title = %s WHERE id = %s::uuid",
            (Jsonb({"changed": True}), "Changed title", resources.first_rule_id),
        )
        connection.commit()

    with pool.connection() as connection:
        snapshot = RunRuleScopeRepository(connection).list_rules_for_run(resources.run_id)[0]

    assert snapshot.title == "Required clause"
    assert snapshot.definition == {"all": ["governing_law", {"required": True}]}


def test_snapshot_insert_rejects_cross_pile_rule_set_and_mismatched_source_pair() -> None:
    resources = create_resources()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        with pytest.raises(RaiseException, match="must belong to run"):
            repository.insert_rule_set_snapshot(
                RunRuleSetSnapshot(
                    run_id=resources.run_id,
                    source_rule_set_id=resources.other_rule_set_id,
                    name="Other rules",
                    description="Other pile rules",
                    version=1,
                ),
            )
        connection.rollback()

        repository.insert_rule_set_snapshot(rule_set_snapshot(resources))
        with pytest.raises(RaiseException, match="does not belong"):
            repository.insert_rule_snapshot(
                RunRuleSnapshot(
                    run_id=resources.run_id,
                    source_rule_id=resources.other_rule_id,
                    source_rule_set_id=resources.rule_set_id,
                    rule_key="other-rule",
                    title="Other rule",
                    severity="LOW",
                    definition={"outside": True},
                ),
            )
        connection.rollback()


def test_duplicate_snapshot_and_caller_rollback_backstops() -> None:
    resources = create_resources()

    with pool.connection() as connection:
        repository = RunRuleScopeRepository(connection)
        repository.insert_rule_set_snapshot(rule_set_snapshot(resources))
        with pytest.raises(UniqueViolation):
            repository.insert_rule_set_snapshot(rule_set_snapshot(resources))
        connection.rollback()

        repository.insert_rule_set_snapshot(rule_set_snapshot(resources))
        repository.complete_scope(resources.run_id)
        connection.rollback()

    with pool.connection() as connection:
        assert RunRuleScopeRepository(connection).get_scope(resources.run_id) is None


def test_header_and_child_inserts_serialize_on_the_run_lock() -> None:
    resources = create_resources()

    with pool.connection() as first_connection, pool.connection() as second_connection:
        first_repository = RunRuleScopeRepository(first_connection)
        second_repository = RunRuleScopeRepository(second_connection)
        first_repository.insert_rule_set_snapshot(rule_set_snapshot(resources))

        second_connection.execute("SET LOCAL lock_timeout = '100ms'")
        with pytest.raises(LockNotAvailable):
            second_repository.complete_scope(resources.run_id)
        second_connection.rollback()

        first_repository.complete_scope(resources.run_id)
        first_connection.commit()

        with pytest.raises(RaiseException, match="already closed"):
            second_repository.insert_rule_snapshot(
                rule_snapshot(resources, resources.first_rule_id),
            )
        second_connection.rollback()
