from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import LockNotAvailable
from psycopg.types.json import Jsonb

from db.pool import pool
from repositories.rule_source_repository import RuleSourceRepository


@dataclass(frozen=True)
class SourceResources:
    pile_id: str
    rule_set_id: str
    first_rule_id: str
    second_rule_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> SourceResources:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Rule source repository {uuid4()}",),
        ).fetchone()
        assert pile is not None
        rule_set = connection.execute(
            """
            INSERT INTO rule_sets (pile_id, name, description, version)
            VALUES (%s::uuid, %s, %s, 2)
            RETURNING id
            """,
            (str(pile[0]), "Source rules", "Used for capture",),
        ).fetchone()
        assert rule_set is not None
        rule_ids = []
        for key, definition in (("z-rule", {"z": True}), ("a-rule", {"nested": [1, 2]})):
            row = connection.execute(
                """
                INSERT INTO rules (rule_set_id, rule_key, title, severity, definition)
                VALUES (%s::uuid, %s, %s, 'HIGH', %s::jsonb)
                RETURNING id
                """,
                (str(rule_set[0]), key, key.title(), Jsonb(definition)),
            ).fetchone()
            assert row is not None
            rule_ids.append(str(row[0]))
        connection.commit()

    return SourceResources(str(pile[0]), str(rule_set[0]), rule_ids[0], rule_ids[1])


def test_loads_locked_source_rows_in_identifier_order_and_skips_missing(
    resources: SourceResources,
) -> None:
    with pool.connection() as connection:
        repository = RuleSourceRepository(connection)
        rule_sets = repository.load_rule_sets_for_share([str(uuid4()), resources.rule_set_id])
        rules = repository.load_rules_for_share(
            [resources.first_rule_id, str(uuid4()), resources.second_rule_id],
        )
        connection.commit()

    assert len(rule_sets) == 1
    assert rule_sets[0].rule_set_id == resources.rule_set_id
    assert rule_sets[0].pile_id == resources.pile_id
    assert rule_sets[0].version == 2
    assert [record.rule_id for record in rules] == sorted(
        [resources.first_rule_id, resources.second_rule_id],
    )
    assert all(record.rule_set_id == resources.rule_set_id for record in rules)
    assert {str(record.definition) for record in rules} == {"{'z': True}", "{'nested': [1, 2]}"}


def test_shared_source_locks_block_content_mutation(resources: SourceResources) -> None:
    with pool.connection() as first_connection, pool.connection() as second_connection:
        RuleSourceRepository(first_connection).load_rules_for_share([resources.first_rule_id])

        second_connection.execute("SET LOCAL lock_timeout = '100ms'")
        with pytest.raises(LockNotAvailable):
            second_connection.execute(
                "UPDATE rules SET title = 'blocked' WHERE id = %s::uuid",
                (resources.first_rule_id,),
            )
        second_connection.rollback()
        first_connection.commit()

        second_connection.execute(
            "UPDATE rules SET title = 'updated' WHERE id = %s::uuid",
            (resources.first_rule_id,),
        )
        second_connection.rollback()
