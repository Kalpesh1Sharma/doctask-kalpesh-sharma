from __future__ import annotations

from uuid import uuid4

import pytest

from api.application.create_pile_service import CreatePileCommand, CreatePileService
from api.application.create_rule_set_service import (
    CreateRuleCommand,
    CreateRuleSetCommand,
    CreateRuleSetService,
    InvalidCreateRuleSetCommandError,
)
from api.application.external_query_service import ExternalQueryService
from db.pool import pool
from domain.document_version import DocumentProcessingStatus, DocumentVersion
from domain.run import Run, RunStage, RunType
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.run_repository import RunRepository
from repositories.run_step_repository import RunStepRepository


@pytest.fixture(scope="module", autouse=True)
def _open_pool() -> None:
    pool.open()


@pytest.fixture
def created_pile_id() -> str:
    created = CreatePileService(pool).create(
        CreatePileCommand(
            name=f"Phase 6B pile {uuid4()}",
            description="REST application facade fixture",
        ),
    )
    try:
        yield created.pile_id
    finally:
        with pool.connection() as connection:
            connection.execute(
                "UPDATE piles SET latest_run_id = NULL WHERE id = %s::uuid",
                (created.pile_id,),
            )
            connection.execute(
                "DELETE FROM run_metrics WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (created.pile_id,),
            )
            connection.execute(
                "DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (created.pile_id,),
            )
            connection.execute(
                "DELETE FROM run_document_versions WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (created.pile_id,),
            )
            connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute(
                "DELETE FROM document_chunks WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))",
                (created.pile_id,),
            )
            connection.execute(
                "UPDATE documents SET current_version_id = NULL WHERE pile_id = %s::uuid",
                (created.pile_id,),
            )
            connection.execute(
                "DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid)",
                (created.pile_id,),
            )
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute(
                "DELETE FROM rules WHERE rule_set_id IN (SELECT id FROM rule_sets WHERE pile_id = %s::uuid)",
                (created.pile_id,),
            )
            connection.execute("DELETE FROM rule_sets WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created.pile_id,))
            connection.commit()


def test_create_pile_is_immediately_visible_through_external_query() -> None:
    created = CreatePileService(pool).create(
        CreatePileCommand(
            name=f"External pile {uuid4()}",
            description=" durable ",
            watch_enabled=False,
        ),
    )
    try:
        projected = ExternalQueryService(pool).get_pile(created.pile_id)
        assert projected.pile_id == created.pile_id
        assert projected.name == created.name
        assert projected.description == "durable"
        assert projected.latest_run_id is None
    finally:
        with pool.connection() as connection:
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created.pile_id,))
            connection.commit()


def test_create_rule_set_is_atomic_and_queryable(created_pile_id: str) -> None:
    result = CreateRuleSetService(pool).create(
        CreateRuleSetCommand(
            pile_id=created_pile_id,
            name="Contract playbook",
            description="Rules for Phase 6B",
            version=1,
            rules=(
                CreateRuleCommand(
                    rule_key="payment-term",
                    title="Payment term maximum",
                    description="Flag terms longer than allowed",
                    severity="HIGH",
                    definition={"max_days": 45},
                ),
                CreateRuleCommand(
                    rule_key="governing-law",
                    title="Governing law required",
                    description=None,
                    severity="MEDIUM",
                    definition={"required": True},
                ),
            ),
        ),
    )

    queried = ExternalQueryService(pool).get_rule_set(result.rule_set.rule_set_id)
    listed = ExternalQueryService(pool).list_rule_sets(created_pile_id)

    assert queried.rule_set_id == result.rule_set.rule_set_id
    assert [rule.rule_key for rule in queried.rules] == ["governing-law", "payment-term"]
    assert queried.rules[1].definition == {"max_days": 45}
    assert [item.rule_set_id for item in listed] == [result.rule_set.rule_set_id]


def test_document_and_run_projections_expose_durable_history(created_pile_id: str) -> None:
    with pool.connection() as connection:
        document = DocumentRepository(connection).create(created_pile_id, "master-contract")
        version_id = str(uuid4())
        version = DocumentVersionRepository(connection).create(
            DocumentVersion(
                document_version_id=version_id,
                document_id=document.document_id,
                version_number=1,
                content_hash="sha256:" + "a" * 64,
                storage_uri="source://sha256/" + "a" * 64,
            ),
            "text/plain; charset=utf-8",
            12,
        )
        ready = DocumentVersionRepository(connection).transition_from_pending(
            version.document_version_id,
            DocumentProcessingStatus.READY,
        )
        assert ready is not None
        DocumentRepository(connection).set_current_version(
            document.document_id,
            version.document_version_id,
        )
        run = RunRepository(connection).create(
            Run(run_id=str(uuid4()), pile_id=created_pile_id, run_type=RunType.MANUAL),
        )
        steps = RunStepRepository(connection)
        for stage in RunStage:
            steps.create(run.run_id, stage, 1)
        connection.commit()

    query = ExternalQueryService(pool)
    documents = query.list_documents(created_pile_id)
    versions = query.list_document_versions(document.document_id)
    runs = query.list_runs(created_pile_id)
    projected_run = query.get_run(run.run_id)

    assert [item.document_id for item in documents] == [document.document_id]
    assert versions[0].processing_status is DocumentProcessingStatus.READY
    assert versions[0].document_version_id == version.document_version_id
    assert [item.run_id for item in runs] == [run.run_id]
    assert [step.stage for step in projected_run.steps] == list(RunStage)
    assert all(step.attempt == 1 for step in projected_run.steps)



def test_non_json_rule_definition_fails_before_any_rule_set_write(created_pile_id: str) -> None:
    service = CreateRuleSetService(pool)
    command = CreateRuleSetCommand(
        pile_id=created_pile_id,
        name="Invalid definition",
        description=None,
        version=1,
        rules=(
            CreateRuleCommand("bad-json", "Bad JSON", None, "LOW", {"bad": object()}),
        ),
    )

    with pytest.raises(InvalidCreateRuleSetCommandError):
        service.create(command)

    assert ExternalQueryService(pool).list_rule_sets(created_pile_id) == ()


def test_duplicate_rule_keys_fail_before_any_rule_set_write(created_pile_id: str) -> None:
    service = CreateRuleSetService(pool)
    command = CreateRuleSetCommand(
        pile_id=created_pile_id,
        name="Invalid duplicate keys",
        description=None,
        version=1,
        rules=(
            CreateRuleCommand("same", "One", None, "LOW", {"a": 1}),
            CreateRuleCommand("same", "Two", None, "LOW", {"b": 2}),
        ),
    )

    with pytest.raises(InvalidCreateRuleSetCommandError):
        service.create(command)

    assert ExternalQueryService(pool).list_rule_sets(created_pile_id) == ()
