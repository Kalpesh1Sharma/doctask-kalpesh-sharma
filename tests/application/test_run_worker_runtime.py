from __future__ import annotations

from uuid import uuid4

import pytest

from db.pool import pool
from domain.run import Run, RunType
from repositories.pile_repository import PileRepository
from repositories.rule_set_repository import RuleSetRepository
from repositories.run_repository import RunRepository
from runtime.run_worker import (
    postgres_worker_candidate_loader,
    postgres_worker_rule_scope_loader,
)


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def worker_pile() -> str:
    with pool.connection() as connection:
        pile = PileRepository(connection).create(
            f"worker-runtime-{uuid4()}",
            None,
            watch_enabled=False,
            watch_location=None,
        )
        connection.commit()
    try:
        yield pile.pile_id
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM runs WHERE pile_id = %s::uuid",
                (pile.pile_id,),
            )
            connection.execute(
                """
                DELETE FROM rules
                WHERE rule_set_id IN (
                    SELECT id FROM rule_sets WHERE pile_id = %s::uuid
                )
                """,
                (pile.pile_id,),
            )
            connection.execute(
                "DELETE FROM rule_sets WHERE pile_id = %s::uuid",
                (pile.pile_id,),
            )
            connection.execute(
                "DELETE FROM piles WHERE id = %s::uuid",
                (pile.pile_id,),
            )
            connection.commit()


def test_postgres_worker_selects_latest_version_of_each_ruleset_name(worker_pile: str) -> None:
    with pool.connection() as connection:
        repository = RuleSetRepository(connection)
        old = repository.create_rule_set(worker_pile, "compliance", None, 1)
        old_rule = repository.create_rule(
            old.rule_set_id,
            rule_key="old",
            title="Old",
            description=None,
            severity="LOW",
            definition={"kind": "old"},
        )
        current = repository.create_rule_set(worker_pile, "compliance", None, 2)
        current_rule = repository.create_rule(
            current.rule_set_id,
            rule_key="current",
            title="Current",
            description=None,
            severity="HIGH",
            definition={"kind": "current"},
        )
        secondary = repository.create_rule_set(worker_pile, "disclosure", None, 1)
        secondary_rule = repository.create_rule(
            secondary.rule_set_id,
            rule_key="disclosure",
            title="Disclosure",
            description=None,
            severity="MEDIUM",
            definition={"kind": "disclosure"},
        )
        run = RunRepository(connection).create(
            Run(
                run_id=str(uuid4()),
                pile_id=worker_pile,
                run_type=RunType.MANUAL,
            ),
        )
        connection.commit()

    selection = postgres_worker_rule_scope_loader(pool)(run)

    assert selection is not None
    assert set(selection.source_rule_set_ids) == {
        current.rule_set_id,
        secondary.rule_set_id,
    }
    assert set(selection.source_rule_ids) == {
        current_rule.rule_id,
        secondary_rule.rule_id,
    }
    assert old.rule_set_id not in selection.source_rule_set_ids
    assert old_rule.rule_id not in selection.source_rule_ids


def test_postgres_worker_candidate_loader_discovers_pending_run(worker_pile: str) -> None:
    run_id = str(uuid4())
    with pool.connection() as connection:
        RunRepository(connection).create(
            Run(run_id=run_id, pile_id=worker_pile, run_type=RunType.INITIAL),
        )
        connection.commit()

    with pool.connection() as connection:
        record = RunRepository(connection).get_by_id(run_id)
    assert record is not None

    selection = postgres_worker_rule_scope_loader(pool)(record)
    candidates = postgres_worker_candidate_loader(pool, limit=10_000)()

    assert selection is not None
    assert selection.source_rule_set_ids == ()
    assert selection.source_rule_ids == ()
    assert run_id in {candidate.run_id for candidate in candidates}
