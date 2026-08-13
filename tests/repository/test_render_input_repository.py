from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from db.pool import pool
from domain.evidence import EvidenceSpan
from domain.fact import Fact
from domain.finding import Finding, FindingStatus, FindingType
from domain.render_input import build_render_input_manifest
from domain.review_state import ReviewStateMembership, build_review_state_snapshot
from domain.run_membership import RunMembershipRelation
from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.fact_repository import FactRepository
from repositories.finding_repository import FindingRepository
from repositories.render_input_repository import RenderInputRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository


@dataclass(frozen=True)
class Resources:
    pile_id: str
    origin_run_id: str
    run_id: str
    empty_run_id: str
    document_version_id: str
    second_document_version_id: str
    rule_set_id: str
    rule_id: str
    current_rule_set_id: str
    current_rule_id: str
    selected_empty_rule_set_id: str
    evidence_id: str
    second_evidence_id: str
    fact_id: str
    created_fact_id: str
    finding_id: str
    current_finding_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> Resources:
    with pool.connection() as connection:
        pile_id = create_pile(connection)
        origin_run_id = create_run(connection, pile_id)
        run_id = create_run(connection, pile_id)
        empty_run_id = create_run(connection, pile_id)
        rule_set_id = create_rule_set(connection, pile_id, "Historical rule set", 1)
        rule_id = create_rule(connection, rule_set_id, "notice", "Historical notice", {"days": 30})
        current_rule_set_id = create_rule_set(connection, pile_id, "Current rule set", 1)
        current_rule_id = create_rule(
            connection,
            current_rule_set_id,
            "termination",
            "Current termination rule",
            {"days": 14},
        )
        selected_empty_rule_set_id = create_rule_set(connection, pile_id, "Selected empty rule set", 1)
        capture_scope(
            connection,
            origin_run_id,
            ((rule_set_id, "Historical rule set"),),
            ((rule_id, rule_set_id, "notice", "Historical notice", {"days": 30}),),
        )
        capture_scope(
            connection,
            run_id,
            (
                (current_rule_set_id, "Current rule set"),
                (selected_empty_rule_set_id, "Selected empty rule set"),
            ),
            ((current_rule_id, current_rule_set_id, "termination", "Current termination rule", {"days": 14}),),
        )
        capture_empty_scope(connection, empty_run_id)
        document_version_id, chunk_id = create_document_version_and_chunk(connection, pile_id)
        second_document_version_id, second_chunk_id = create_document_version_and_chunk(connection, pile_id)
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (run_id, document_version_id),
        )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (run_id, second_document_version_id),
        )
        evidence_id = str(uuid4())
        EvidenceSpanRepository(connection).create(
            EvidenceSpan(
                evidence_id=evidence_id,
                document_version_id=document_version_id,
                chunk_id=chunk_id,
                page_number=1,
                start_offset=0,
                end_offset=8,
                source_text="notice text",
                content_hash="sha256:notice-text",
            ),
        )
        second_evidence_id = str(uuid4())
        EvidenceSpanRepository(connection).create(
            EvidenceSpan(
                evidence_id=second_evidence_id,
                document_version_id=second_document_version_id,
                chunk_id=second_chunk_id,
                page_number=1,
                start_offset=0,
                end_offset=8,
                source_text="termination text",
                content_hash="sha256:termination-text",
            ),
        )
        fact_id = str(uuid4())
        FactRepository(connection).create(
            Fact(
                fact_id=fact_id,
                origin_run_id=origin_run_id,
                subject="vendor",
                predicate="notice_period",
                object_value={"days": [30]},
            ),
        )
        RunFactRepository(connection).add(run_id, fact_id, RunMembershipRelation.CARRIED_FORWARD)
        created_fact_id = str(uuid4())
        FactRepository(connection).create(
            Fact(
                fact_id=created_fact_id,
                origin_run_id=run_id,
                subject="vendor",
                predicate="termination_period",
                object_value={"days": [14]},
            ),
        )
        RunFactRepository(connection).add(run_id, created_fact_id, RunMembershipRelation.CREATED)
        connection.execute(
            "INSERT INTO fact_evidence (fact_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (fact_id, evidence_id),
        )
        connection.execute(
            "INSERT INTO fact_evidence (fact_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (created_fact_id, second_evidence_id),
        )
        finding_id = str(uuid4())
        FindingRepository(connection).create(
            Finding(
                finding_id=finding_id,
                origin_run_id=origin_run_id,
                finding_type=FindingType.RULE_VIOLATION,
                rule_id=rule_id,
                title="Missing notice",
                severity="HIGH",
                status=FindingStatus.APPROVED,
            ),
        )
        RunFindingRepository(connection).add(run_id, finding_id, RunMembershipRelation.CARRIED_FORWARD)
        current_finding_id = str(uuid4())
        FindingRepository(connection).create(
            Finding(
                finding_id=current_finding_id,
                origin_run_id=run_id,
                finding_type=FindingType.RULE_VIOLATION,
                rule_id=current_rule_id,
                title="Current termination issue",
                severity="HIGH",
                status=FindingStatus.APPROVED,
            ),
        )
        RunFindingRepository(connection).add(run_id, current_finding_id, RunMembershipRelation.CREATED)
        connection.execute(
            "INSERT INTO finding_evidence (finding_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (finding_id, evidence_id),
        )
        connection.execute(
            "INSERT INTO finding_evidence (finding_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
            (current_finding_id, second_evidence_id),
        )
        connection.commit()
    return Resources(
        pile_id,
        origin_run_id,
        run_id,
        empty_run_id,
        document_version_id,
        second_document_version_id,
        rule_set_id,
        rule_id,
        current_rule_set_id,
        current_rule_id,
        selected_empty_rule_set_id,
        evidence_id,
        second_evidence_id,
        fact_id,
        created_fact_id,
        finding_id,
        current_finding_id,
    )


def test_loads_complete_normalized_source_with_carried_origin_context(resources: Resources) -> None:
    with pool.connection() as connection:
        memberships = RunFindingRepository(connection).list_review_state_for_run(resources.run_id)
        review_state = build_review_state_snapshot(
            resources.run_id,
            [
                ReviewStateMembership(
                    finding_id=record.finding_id,
                    relation=record.relation,
                    origin_run_id=record.origin_run_id,
                    finding_type=record.finding_type,
                    rule_id=record.rule_id,
                    title=record.title,
                    description=record.description,
                    severity=record.severity,
                    status=record.status,
                )
                for record in memberships
            ],
        )
        source_record = RenderInputRepository(connection).load_for_run(resources.run_id, review_state)
        connection.commit()

    source = source_record.to_domain()
    manifest = build_render_input_manifest(resources.run_id, review_state, source, source_record.rule_scope)
    assert [document.document_version_id for document in manifest.source.documents] == sorted(
        (resources.document_version_id, resources.second_document_version_id),
    )
    assert {fact.relation for fact in manifest.source.facts} == {
        RunMembershipRelation.CREATED,
        RunMembershipRelation.CARRIED_FORWARD,
    }
    assert [fact.fact_id for fact in manifest.source.facts] == sorted(
        (resources.fact_id, resources.created_fact_id),
    )
    assert [span.evidence_id for span in manifest.source.evidence_spans] == sorted(
        (resources.evidence_id, resources.second_evidence_id),
    )
    assert [reference.fact_id for reference in manifest.source.fact_evidence] == sorted(
        (resources.fact_id, resources.created_fact_id),
    )
    assert [reference.finding_id for reference in manifest.source.finding_evidence] == sorted(
        (resources.finding_id, resources.current_finding_id),
    )
    assert [rule_set.source_rule_set_id for rule_set in manifest.source.evaluated_rule_sets] == sorted(
        (resources.current_rule_set_id, resources.selected_empty_rule_set_id),
    )
    assert {rule_set.name for rule_set in manifest.source.evaluated_rule_sets} == {
        "Current rule set",
        "Selected empty rule set",
    }
    assert manifest.source.evaluated_rules[0].source_rule_id == resources.current_rule_id
    assert manifest.source.finding_origin_rule_sets[0].source_rule_set_id == resources.rule_set_id
    assert manifest.source.finding_origin_rules[0].source_rule_id == resources.rule_id


def test_rule_snapshots_and_excluded_document_metadata_ignore_source_mutation(resources: Resources) -> None:
    with pool.connection() as connection:
        connection.execute(
            "UPDATE rule_sets SET name = 'Mutable replacement' WHERE id = %s::uuid",
            (resources.rule_set_id,),
        )
        connection.execute(
            "UPDATE rules SET title = 'Mutable replacement' WHERE id = %s::uuid",
            (resources.rule_id,),
        )
        connection.execute(
            "UPDATE rule_sets SET name = 'Mutable current set' WHERE id = %s::uuid",
            (resources.current_rule_set_id,),
        )
        connection.execute(
            "UPDATE rules SET title = 'Mutable current rule' WHERE id = %s::uuid",
            (resources.current_rule_id,),
        )
        before_metadata_change = build_source_and_manifest(connection, resources.run_id)
        connection.execute(
            """
            UPDATE documents
            SET logical_key = 'renamed-logical-document'
            WHERE id = (
                SELECT document_id FROM document_versions WHERE id = %s::uuid
            )
            """,
            (resources.document_version_id,),
        )
        connection.commit()
        after_metadata_change = build_source_and_manifest(connection, resources.run_id)

    source = after_metadata_change[0]
    assert source.finding_origin_rule_sets[0].name == "Historical rule set"
    assert source.finding_origin_rules[0].title == "Historical notice"
    assert {rule_set.name for rule_set in source.evaluated_rule_sets} == {
        "Current rule set",
        "Selected empty rule set",
    }
    assert source.evaluated_rules[0].title == "Current termination rule"
    assert before_metadata_change[1] == after_metadata_change[1]


def test_uppercase_run_id_keeps_current_rule_violation_current(resources: Resources) -> None:
    with pool.connection() as connection:
        source, manifest = build_source_and_manifest(connection, resources.run_id.upper())

    assert manifest.run_id == resources.run_id
    assert source.evaluated_rules[0].source_rule_id == resources.current_rule_id
    assert [(rule.origin_run_id, rule.source_rule_id) for rule in source.finding_origin_rules] == [
        (resources.origin_run_id, resources.rule_id),
    ]


def test_unknown_and_known_empty_reads_remain_ambiguous(resources: Resources) -> None:
    unknown_run_id = str(uuid4())
    review_state = build_review_state_snapshot(unknown_run_id, [])
    with pool.connection() as connection:
        unknown_source = RenderInputRepository(connection).load_for_run(unknown_run_id, review_state)
        known_source = RenderInputRepository(connection).load_for_run(
            resources.empty_run_id,
            build_review_state_snapshot(resources.empty_run_id, []),
        )

    assert unknown_source.rule_scope is None
    assert known_source.rule_scope is not None
    for source in (unknown_source, known_source):
        assert source.documents == ()
        assert source.facts == ()
        assert source.finding_origin_rules == ()


def test_missing_legacy_origin_context_is_not_loaded_from_mutable_sources(resources: Resources) -> None:
    with pool.connection() as connection:
        uncaptured_origin_run_id = create_run(connection, resources.pile_id)
        review_state = build_review_state_snapshot(
            resources.run_id,
            [
                ReviewStateMembership(
                    finding_id=str(uuid4()),
                    relation=RunMembershipRelation.CARRIED_FORWARD,
                    origin_run_id=uncaptured_origin_run_id,
                    finding_type=FindingType.RULE_VIOLATION,
                    rule_id=resources.current_rule_id,
                    title="Legacy rule violation",
                    description=None,
                    severity="HIGH",
                    status=FindingStatus.APPROVED,
                ),
            ],
        )
        source_record = RenderInputRepository(connection).load_for_run(resources.run_id, review_state)

        assert source_record.finding_origin_rule_sets == ()
        assert source_record.finding_origin_rules == ()
        with pytest.raises(ValueError, match="origin rule snapshots"):
            build_render_input_manifest(
                resources.run_id,
                review_state,
                source_record.to_domain(),
                source_record.rule_scope,
            )


def create_pile(connection) -> str:
    row = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"Render input repository {uuid4()}",),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_run(connection, pile_id: str) -> str:
    row = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
        RETURNING id
        """,
        (pile_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_rule_set(connection, pile_id: str, name: str, version: int) -> str:
    row = connection.execute(
        "INSERT INTO rule_sets (pile_id, name, version) VALUES (%s::uuid, %s, %s) RETURNING id",
        (pile_id, name, version),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_rule(connection, rule_set_id: str, key: str, title: str, definition: object) -> str:
    row = connection.execute(
        """
        INSERT INTO rules (rule_set_id, rule_key, title, severity, definition)
        VALUES (%s::uuid, %s, %s, 'HIGH', %s::jsonb)
        RETURNING id
        """,
        (rule_set_id, key, title, Jsonb(definition)),
    ).fetchone()
    assert row is not None
    return str(row[0])


def capture_scope(
    connection,
    run_id: str,
    rule_sets: tuple[tuple[str, str], ...],
    rules: tuple[tuple[str, str, str, str, object], ...],
) -> None:
    repository = RunRuleScopeRepository(connection)
    for rule_set_id, name in rule_sets:
        repository.insert_rule_set_snapshot(
            RunRuleSetSnapshot(run_id=run_id, source_rule_set_id=rule_set_id, name=name, version=1),
        )
    for rule_id, rule_set_id, rule_key, title, definition in rules:
        repository.insert_rule_snapshot(
            RunRuleSnapshot(
                run_id=run_id,
                source_rule_id=rule_id,
                source_rule_set_id=rule_set_id,
                rule_key=rule_key,
                title=title,
                severity="HIGH",
                definition=definition,
            ),
        )
    repository.complete_scope(run_id)


def build_source_and_manifest(connection, run_id: str):
    normalized_run_id = str(UUID(run_id))
    memberships = RunFindingRepository(connection).list_review_state_for_run(normalized_run_id)
    review_state = build_review_state_snapshot(
        normalized_run_id,
        [
            ReviewStateMembership(
                finding_id=record.finding_id,
                relation=record.relation,
                origin_run_id=record.origin_run_id,
                finding_type=record.finding_type,
                rule_id=record.rule_id,
                title=record.title,
                description=record.description,
                severity=record.severity,
                status=record.status,
            )
            for record in memberships
        ],
    )
    source_record = RenderInputRepository(connection).load_for_run(run_id, review_state)
    source = source_record.to_domain()
    manifest = build_render_input_manifest(run_id, review_state, source, source_record.rule_scope)
    return source, manifest


def capture_empty_scope(connection, run_id: str) -> None:
    RunRuleScopeRepository(connection).complete_scope(run_id)


def create_document_version_and_chunk(connection, pile_id: str) -> tuple[str, str]:
    document = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
        (pile_id, f"document-{uuid4()}"),
    ).fetchone()
    assert document is not None
    version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        )
        VALUES (%s::uuid, 1, %s, %s, 'READY'::document_version_status)
        RETURNING id
        """,
        (str(document[0]), f"sha256:{uuid4().hex}", f"artifact://{uuid4()}"),
    ).fetchone()
    assert version is not None
    chunk = connection.execute(
        """
        INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
        VALUES (%s::uuid, 0, 'notice text', %s)
        RETURNING id
        """,
        (str(version[0]), f"sha256:{uuid4().hex}"),
    ).fetchone()
    assert chunk is not None
    return str(version[0]), str(chunk[0])
