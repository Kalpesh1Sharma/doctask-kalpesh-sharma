import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import uuid4

import pytest
from psycopg.errors import (
    CheckViolation,
    DeadlockDetected,
    DivisionByZero,
    ForeignKeyViolation,
    LockNotAvailable,
    RaiseException,
    SerializationFailure,
)

from api.application import prepare_update_run_service as preparation_module
from api.application.prepare_update_run_service import (
    PrepareUpdateRunCommand,
    PrepareUpdateRunService,
    UpdatePreparationBaselineError,
    UpdatePreparationBaselineScopeError,
    UpdatePreparationClaimLostError,
    UpdatePreparationIdempotencyIntegrityError,
    UpdatePreparationInProgressError,
    UpdatePreparationInputConflictError,
    UpdatePreparationInputDriftError,
    UpdatePreparationPreviouslyFailedError,
    UpdatePreparationRunNotEligibleError,
    UpdatePreparationTargetStateConflictError,
    UpdatePreparationTriggerError,
)
from db.pool import pool
from domain.fact import Fact, FactStatus
from domain.finding import Finding, FindingType
from domain.review_decision import ReviewDecisionType
from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.finding_repository import FindingRepository
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from repositories.review_decision_repository import ReviewDecisionRepository
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRepository


@dataclass(frozen=True)
class Resources:
    pile_id: str
    baseline_run_id: str
    update_run_id: str
    retained_version_id: str
    replaced_version_id: str
    trigger_version_id: str
    retained_evidence_id: str
    replaced_evidence_id: str
    retained_confirmed_id: str
    retained_disputed_id: str
    changed_fact_id: str
    mixed_fact_id: str
    superseded_fact_id: str
    evidence_less_fact_id: str


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> Resources:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"UPDATE preparation {uuid4()}",),
        ).fetchone()
        assert pile is not None
        pile_id = str(pile[0])
        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, completed_at)
            VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now())
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None
        baseline_run_id = str(baseline[0])
        retained_version_id, retained_evidence_id = create_document_version_evidence(
            connection, pile_id, "retained", 1, "retained 100", 9, 12, "100"
        )
        replaced_version_id, replaced_evidence_id = create_document_version_evidence(
            connection, pile_id, "changed", 1, "changed 200", 8, 11, "200"
        )
        trigger_version_id, _ = create_version_for_existing_document(
            connection, replaced_version_id, 2, "changed 201"
        )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)",
            (baseline_run_id, retained_version_id, baseline_run_id, replaced_version_id),
        )
        origin = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'PENDING') RETURNING id",
            (pile_id,),
        ).fetchone()
        assert origin is not None
        retained_confirmed_id = create_fact(
            connection, baseline_run_id, 100, (retained_evidence_id,), FactStatus.CONFIRMED
        )
        retained_disputed_id = create_fact(
            connection, baseline_run_id, 101, (retained_evidence_id,), FactStatus.DISPUTED,
            origin_run_id=str(origin[0]), relation=RunMembershipRelation.CARRIED_FORWARD,
        )
        changed_fact_id = create_fact(
            connection, baseline_run_id, 200, (replaced_evidence_id,), FactStatus.CONFIRMED
        )
        mixed_fact_id = create_fact(
            connection, baseline_run_id, 300, (retained_evidence_id, replaced_evidence_id), FactStatus.CONFIRMED
        )
        superseded_fact_id = create_fact(
            connection, baseline_run_id, 400, (retained_evidence_id,), FactStatus.SUPERSEDED
        )
        evidence_less_fact_id = create_fact(
            connection, baseline_run_id, 500, (), FactStatus.CONFIRMED
        )
        update = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, trigger_version_id, baseline_run_id),
        ).fetchone()
        assert update is not None
        connection.commit()
    value = Resources(
        pile_id, baseline_run_id, str(update[0]), retained_version_id, replaced_version_id,
        trigger_version_id, retained_evidence_id, replaced_evidence_id, retained_confirmed_id,
        retained_disputed_id, changed_fact_id, mixed_fact_id, superseded_fact_id, evidence_less_fact_id,
    )
    try:
        yield value
    finally:
        with pool.connection() as connection:
            connection.execute("DELETE FROM idempotency_records WHERE scope = 'update-preparation-v1' AND key LIKE %s", ("prepare-update:%",))
            connection.execute("DELETE FROM review_decisions WHERE finding_id IN (SELECT id FROM findings WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid))", (value.pile_id,))
            connection.execute("DELETE FROM run_findings WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (value.pile_id,))
            connection.execute("DELETE FROM findings WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (value.pile_id,))
            connection.execute("DELETE FROM fact_evidence WHERE fact_id IN (SELECT id FROM facts WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid))", (value.pile_id,))
            connection.execute("DELETE FROM run_facts WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (value.pile_id,))
            connection.execute("DELETE FROM facts WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (value.pile_id,))
            connection.execute("DELETE FROM run_document_versions WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (value.pile_id,))
            connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (value.pile_id,))
            connection.execute("DELETE FROM evidence_spans WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))", (value.pile_id,))
            connection.execute("DELETE FROM document_chunks WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))", (value.pile_id,))
            connection.execute("DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid)", (value.pile_id,))
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (value.pile_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (value.pile_id,))
            connection.commit()


def create_document_version_evidence(connection, pile_id: str, key: str, number: int, text: str, start: int, end: int, quote: str, *, status: str = "READY") -> tuple[str, str]:
    document = connection.execute("INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id", (pile_id, f"{key}-{uuid4()}")).fetchone()
    assert document is not None
    version = connection.execute("INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri, processing_status) VALUES (%s::uuid, %s, %s, %s, %s::document_version_status) RETURNING id", (str(document[0]), number, f"sha256:{uuid4().hex}", f"storage://{uuid4()}", status)).fetchone()
    assert version is not None
    chunk = connection.execute("INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash) VALUES (%s::uuid, 0, %s, %s) RETURNING id", (str(version[0]), text, f"sha256:{uuid4().hex}")).fetchone()
    assert chunk is not None
    evidence = connection.execute("INSERT INTO evidence_spans (document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash) VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s) RETURNING id", (str(version[0]), str(chunk[0]), start, end, quote, f"sha256:{uuid4().hex}")).fetchone()
    assert evidence is not None
    return str(version[0]), str(evidence[0])


def create_version_for_existing_document(connection, old_version_id: str, number: int, text: str) -> tuple[str, str]:
    row = connection.execute("SELECT document_id FROM document_versions WHERE id = %s::uuid", (old_version_id,)).fetchone()
    assert row is not None
    version = connection.execute("INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri, processing_status) VALUES (%s::uuid, %s, %s, %s, 'READY') RETURNING id", (str(row[0]), number, f"sha256:{uuid4().hex}", f"storage://{uuid4()}")).fetchone()
    assert version is not None
    return str(version[0]), ""


def create_fact(connection, run_id: str, value: int, evidence_ids: tuple[str, ...], status: FactStatus, *, origin_run_id: str | None = None, relation: RunMembershipRelation = RunMembershipRelation.CREATED) -> str:
    record = FactRepository(connection).create(Fact(fact_id=str(uuid4()), origin_run_id=origin_run_id or run_id, subject="amount", predicate="equals", object_value=value, status=status))
    RunFactRepository(connection).add(run_id, record.fact_id, relation)
    for evidence_id in evidence_ids:
        FactEvidenceRepository(connection).add(record.fact_id, evidence_id)
    return record.fact_id


def service() -> PrepareUpdateRunService:
    return PrepareUpdateRunService(pool)


def preparation_record(run_id: str):
    with pool.connection() as connection:
        return IdempotencyRepository(connection).get("update-preparation-v1", f"prepare-update:{run_id}")


def target_state(run_id: str) -> tuple[list[str], list[str]]:
    with pool.connection() as connection:
        versions = RunDocumentVersionRepository(connection).list_for_run(run_id)
        facts = RunFactRepository(connection).list_for_run(run_id)
    return [record.document_version_id for record in versions], [record.fact_id for record in facts]


def insert_update_run(connection, pile_id: str, baseline_run_id: str | None, trigger_version_id: str, *, blocked_on_run_id: str | None = None) -> str:
    row = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id, blocked_on_run_id)
        VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid, %s::uuid)
        RETURNING id
        """,
        (pile_id, trigger_version_id, baseline_run_id, blocked_on_run_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_replacement_materializes_scope_and_carries_only_safe_facts(resources: Resources) -> None:
    result = service().prepare(PrepareUpdateRunCommand(resources.update_run_id.upper()))
    assert result.replayed is False
    assert {(change.kind.value, change.target_document_version_id) for change in result.manifest.document_changes} == {
        ("REPLACED", resources.trigger_version_id),
        ("RETAINED", resources.retained_version_id),
    }
    versions, facts = target_state(resources.update_run_id)
    assert set(versions) == {resources.retained_version_id, resources.trigger_version_id}
    assert facts == sorted([resources.retained_confirmed_id, resources.retained_disputed_id])
    with pool.connection() as connection:
        relations = connection.execute("SELECT fact_id, relation FROM run_facts WHERE run_id = %s::uuid ORDER BY fact_id", (resources.update_run_id,)).fetchall()
        origin = connection.execute("SELECT origin_run_id FROM facts WHERE id = %s::uuid", (resources.retained_disputed_id,)).fetchone()
        findings = connection.execute("SELECT count(*) FROM run_findings WHERE run_id = %s::uuid", (resources.update_run_id,)).fetchone()
    assert all(row[1] == "CARRIED_FORWARD" for row in relations)
    assert origin is not None and str(origin[0]) != resources.baseline_run_id
    assert findings == (0,)
    record = preparation_record(resources.update_run_id)
    assert record is not None and record.status is IdempotencyStatus.COMPLETED
    assert record.result_reference == f"update-preparation:{resources.update_run_id}:{result.input_fingerprint}"


def test_added_document_materializes_retained_baseline_plus_trigger(resources: Resources) -> None:
    with pool.connection() as connection:
        added_version, _ = create_document_version_evidence(
            connection, resources.pile_id, "added", 1, "added 600", 6, 9, "600"
        )
        update = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (resources.pile_id, added_version, resources.baseline_run_id),
        ).fetchone()
        assert update is not None
        connection.commit()
    update_id = str(update[0])
    result = service().prepare(PrepareUpdateRunCommand(update_id))
    assert {change.kind.value for change in result.manifest.document_changes} == {"ADDED", "RETAINED"}
    assert set(target_state(update_id)[0]) == {
        resources.retained_version_id, resources.replaced_version_id, added_version,
    }
    assert set(target_state(update_id)[1]) == {
        resources.retained_confirmed_id,
        resources.retained_disputed_id,
        resources.changed_fact_id,
        resources.mixed_fact_id,
    }


@pytest.mark.parametrize("membership", ["document", "fact"])
def test_fresh_preexisting_target_state_conflicts_without_claim_or_writes(resources: Resources, membership: str) -> None:
    with pool.connection() as connection:
        if membership == "document":
            RunDocumentVersionRepository(connection).add(resources.update_run_id, resources.retained_version_id)
        else:
            RunFactRepository(connection).add(resources.update_run_id, resources.retained_confirmed_id, RunMembershipRelation.CARRIED_FORWARD)
        connection.commit()
    before = target_state(resources.update_run_id)
    with pytest.raises(UpdatePreparationTargetStateConflictError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert preparation_record(resources.update_run_id) is None
    assert target_state(resources.update_run_id) == before


def test_completed_replay_and_fresh_status_rejection(resources: Resources) -> None:
    first = service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        RunRepository(connection).update_status(resources.update_run_id, RunStatus.RUNNING)
        connection.commit()
    replay = service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert replay.replayed and replay.input_fingerprint == first.input_fingerprint


def test_fresh_non_pending_update_is_rejected_without_claim(resources: Resources) -> None:
    with pool.connection() as connection:
        connection.execute("UPDATE runs SET status = 'RUNNING' WHERE id = %s::uuid", (resources.update_run_id,))
        connection.commit()
    with pytest.raises(UpdatePreparationRunNotEligibleError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert preparation_record(resources.update_run_id) is None


def test_fresh_eligibility_defensive_seams_and_ddl_backstops(
    resources: Resources,
) -> None:
    """Exercise application-only states that V2.7 intentionally cannot persist."""
    with pool.connection() as connection:
        stored = RunRepository(connection).get_by_id(resources.update_run_id)
        assert stored is not None
        manual_record = replace(stored, run_type=RunType.MANUAL)
        blocked_record = replace(stored, blocked_on_run_id=str(uuid4()))

        with pytest.raises(UpdatePreparationRunNotEligibleError):
            service()._validate_fresh_run(manual_record)
        with pytest.raises(UpdatePreparationRunNotEligibleError):
            service()._validate_fresh_run(blocked_record)

        blocker = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'PENDING') RETURNING id",
            (resources.pile_id,),
        ).fetchone()
        assert blocker is not None
        connection.commit()

        with pytest.raises(CheckViolation), connection.transaction():
            connection.execute(
                """
                INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id)
                VALUES (%s::uuid, 'MANUAL', 'PENDING', %s::uuid)
                """,
                (resources.pile_id, resources.trigger_version_id),
            )
        with pytest.raises(RaiseException), connection.transaction():
            connection.execute(
                """
                INSERT INTO runs (
                    pile_id, run_type, status, trigger_document_version_id,
                    baseline_run_id, blocked_on_run_id
                )
                VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid, %s::uuid)
                """,
                (
                    resources.pile_id,
                    resources.trigger_version_id,
                    resources.baseline_run_id,
                    str(blocker[0]),
                ),
            )


def test_missing_update_lineage_rejects_without_claim(resources: Resources) -> None:
    with pool.connection() as connection:
        missing_trigger, _ = create_document_version_evidence(
            connection, resources.pile_id, "missing-baseline", 1, "m", 0, 1, "m"
        )
        missing_baseline = insert_update_run(
            connection, resources.pile_id, None, missing_trigger
        )
        connection.commit()
    with pytest.raises(UpdatePreparationRunNotEligibleError):
        service().prepare(PrepareUpdateRunCommand(missing_baseline))
    assert preparation_record(missing_baseline) is None


def test_non_completed_baseline_and_unknown_trigger_validation_seams_use_real_source_rows(
    resources: Resources,
) -> None:
    with pool.connection() as connection:
        pending = connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'PENDING') RETURNING id",
            (resources.pile_id,),
        ).fetchone()
        assert pending is not None
        connection.commit()

    pending_baseline_id = str(pending[0])
    with pool.connection() as connection, pytest.raises(RaiseException), connection.transaction():
        connection.execute(
            "INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id) VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)",
            (resources.pile_id, resources.trigger_version_id, pending_baseline_id),
        )

    with pool.connection() as connection:
        stored = RunRepository(connection).get_by_id(resources.update_run_id)
        assert stored is not None
        with pytest.raises(ForeignKeyViolation), connection.transaction():
            connection.execute(
                "INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id) VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid)",
                (resources.pile_id, str(uuid4())),
            )
        pending_record = replace(stored, baseline_run_id=pending_baseline_id)
        unknown_trigger_record = replace(stored, trigger_document_version_id=str(uuid4()))
        with pytest.raises(UpdatePreparationBaselineError):
            service()._build_manifest(connection, pending_record)
        with pytest.raises(UpdatePreparationTriggerError):
            service()._build_manifest(connection, unknown_trigger_record)


def test_trigger_not_ready_cross_pile_and_nonadvancing_are_rejected(resources: Resources) -> None:
    with pool.connection() as connection:
        pending_version, _ = create_document_version_evidence(
            connection, resources.pile_id, "pending", 1, "pending", 0, 1, "p", status="PENDING"
        )
        not_ready = insert_update_run(connection, resources.pile_id, resources.baseline_run_id, pending_version)
        other_pile = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"other-{uuid4()}",)).fetchone()
        assert other_pile is not None
        cross_version, _ = create_document_version_evidence(connection, str(other_pile[0]), "foreign", 1, "foreign", 0, 1, "f")
        cross = insert_update_run(connection, resources.pile_id, resources.baseline_run_id, cross_version)
        older_version, _ = create_document_version_evidence(connection, resources.pile_id, "older", 1, "old", 0, 1, "o")
        newer_version, _ = create_version_for_existing_document(connection, older_version, 2, "new")
        connection.execute("INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)", (resources.baseline_run_id, newer_version))
        nonadvancing = insert_update_run(connection, resources.pile_id, resources.baseline_run_id, older_version)
        connection.commit()
    for run_id in (not_ready, cross, nonadvancing):
        with pytest.raises(UpdatePreparationTriggerError):
            service().prepare(PrepareUpdateRunCommand(run_id))
        assert preparation_record(run_id) is None


def test_baseline_not_ready_cross_pile_and_duplicate_scope_are_separate_failures(
    resources: Resources,
) -> None:
    with pool.connection() as connection:
        pending_version, _ = create_document_version_evidence(connection, resources.pile_id, "baseline-pending", 1, "p", 0, 1, "p", status="PENDING")
        connection.execute("INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)", (resources.baseline_run_id, pending_version))
        connection.commit()
    with pytest.raises(UpdatePreparationBaselineScopeError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        connection.execute("DELETE FROM run_document_versions WHERE run_id = %s::uuid AND document_version_id = %s::uuid", (resources.baseline_run_id, pending_version))
        other_pile = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"scope-other-{uuid4()}",)).fetchone()
        assert other_pile is not None
        foreign_version, _ = create_document_version_evidence(connection, str(other_pile[0]), "foreign-scope", 1, "f", 0, 1, "f")
        connection.execute("INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)", (resources.baseline_run_id, foreign_version))
        connection.commit()
    with pytest.raises(UpdatePreparationBaselineScopeError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        connection.execute("DELETE FROM run_document_versions WHERE run_id = %s::uuid AND document_version_id = %s::uuid", (resources.baseline_run_id, foreign_version))
        connection.execute("INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)", (resources.baseline_run_id, resources.trigger_version_id))
        connection.commit()
    with pytest.raises(UpdatePreparationBaselineScopeError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))


def test_trigger_and_baseline_scope_errors_fail_before_claim(resources: Resources) -> None:
    with pool.connection() as connection:
        duplicate = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, trigger_document_version_id, baseline_run_id)
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (resources.pile_id, resources.replaced_version_id, resources.baseline_run_id),
        ).fetchone()
        assert duplicate is not None
        connection.commit()
    duplicate_id = str(duplicate[0])
    with pytest.raises(UpdatePreparationTriggerError):
        service().prepare(PrepareUpdateRunCommand(duplicate_id))
    assert preparation_record(duplicate_id) is None

    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (resources.baseline_run_id, resources.trigger_version_id),
        )
        connection.commit()
    with pytest.raises(UpdatePreparationBaselineScopeError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert preparation_record(resources.update_run_id) is None


def test_completed_reference_errors_and_existing_claim_outcomes(resources: Resources) -> None:
    for status, reference, error in [
        ("COMPLETED", "malformed", UpdatePreparationIdempotencyIntegrityError),
        ("COMPLETED", f"update-preparation:{uuid4()}:sha256:{'0' * 64}", UpdatePreparationInputConflictError),
    ]:
        with pool.connection() as connection:
            connection.execute("INSERT INTO idempotency_records (scope, key, status, result_reference, claimed_at, completed_at) VALUES (%s, %s, %s::idempotency_status, %s, clock_timestamp(), now())", ("update-preparation-v1", f"prepare-update:{resources.update_run_id}", status, reference))
            connection.commit()
        with pytest.raises(error):
            service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
        with pool.connection() as connection:
            connection.execute("DELETE FROM idempotency_records WHERE scope = %s AND key = %s", ("update-preparation-v1", f"prepare-update:{resources.update_run_id}"))
            connection.commit()
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim("update-preparation-v1", f"prepare-update:{resources.update_run_id}")
        assert claimed is not None and claimed.claimed_at is not None
        connection.commit()
    with pytest.raises(UpdatePreparationInProgressError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))

    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get("update-preparation-v1", f"prepare-update:{resources.update_run_id}")
        assert current is not None and current.claimed_at is not None
        IdempotencyRepository(connection).mark_failed("update-preparation-v1", f"prepare-update:{resources.update_run_id}", current.claim_generation)
        connection.commit()
    with pytest.raises(UpdatePreparationPreviouslyFailedError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))


def test_fresh_owner_precedes_manifest_reconstruction_and_stale_owner_does_not_reclaim(
    resources: Resources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "update-preparation-v1"
    key = f"prepare-update:{resources.update_run_id}"
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.commit()

    def invalid_manifest(*_args):
        raise UpdatePreparationBaselineError("current baseline is invalid")

    monkeypatch.setattr(PrepareUpdateRunService, "_build_manifest", invalid_manifest)
    with pytest.raises(UpdatePreparationInProgressError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        unchanged = IdempotencyRepository(connection).get(scope, key)
        assert unchanged is not None
        assert unchanged.claim_generation == record.claim_generation
        assert unchanged.claimed_at == record.claimed_at
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, key),
        )
        connection.commit()
    with pytest.raises(UpdatePreparationBaselineError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        unchanged = IdempotencyRepository(connection).get(scope, key)
        assert unchanged is not None
        assert unchanged.claim_generation == record.claim_generation


def test_stale_claim_with_target_state_conflict_does_not_reclaim(resources: Resources) -> None:
    scope = "update-preparation-v1"
    key = f"prepare-update:{resources.update_run_id}"
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, key),
        )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (resources.update_run_id, resources.trigger_version_id),
        )
        connection.commit()
    with pool.connection() as connection:
        aged = IdempotencyRepository(connection).get(scope, key)
    assert aged is not None and aged.claimed_at is not None
    with pytest.raises(UpdatePreparationTargetStateConflictError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, key)
    assert current is not None
    assert current.status is IdempotencyStatus.IN_PROGRESS
    assert current.claim_generation == aged.claim_generation
    assert current.claimed_at == aged.claimed_at


def test_stale_claim_and_staged_database_failure_roll_back(resources: Resources, monkeypatch: pytest.MonkeyPatch) -> None:
    original = preparation_module.IdempotencyRepository.mark_completed
    stale_generation: list[int] = []

    def reclaim_before_complete(repository, *args):
        with pool.connection() as other:
            current = IdempotencyRepository(other).get("update-preparation-v1", f"prepare-update:{resources.update_run_id}")
            assert current is not None and current.claimed_at is not None
            stale_generation.append(current.claim_generation)
            other.execute("UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s", ("update-preparation-v1", f"prepare-update:{resources.update_run_id}"))
            replacement = IdempotencyRepository(other).reclaim_stale(
                "update-preparation-v1",
                f"prepare-update:{resources.update_run_id}",
                current.claim_generation,
                timedelta(minutes=5),
            )
            assert replacement is not None
            assert replacement.claim_generation == current.claim_generation + 1
            other.commit()
        return original(repository, *args)

    monkeypatch.setattr(preparation_module.IdempotencyRepository, "mark_completed", reclaim_before_complete)
    with pytest.raises(UpdatePreparationClaimLostError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))

    assert stale_generation
    assert target_state(resources.update_run_id) == ([], [])
    replacement = preparation_record(resources.update_run_id)
    assert replacement is not None
    assert replacement.status is IdempotencyStatus.IN_PROGRESS
    assert replacement.claim_generation == stale_generation[0] + 1

    monkeypatch.setattr(preparation_module.IdempotencyRepository, "mark_completed", original)
    with pool.connection() as connection:
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            ("update-preparation-v1", f"prepare-update:{resources.update_run_id}"),
        )
        connection.commit()

    recovered = service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert recovered.replayed is False
    final = preparation_record(resources.update_run_id)
    assert final is not None
    assert final.status is IdempotencyStatus.COMPLETED
    assert final.claim_generation > stale_generation[0]

    expected_versions = sorted(
        change.target_document_version_id for change in recovered.manifest.document_changes
    )
    expected_facts = sorted(recovered.manifest.carried_fact_ids)
    versions, facts = target_state(resources.update_run_id)
    assert sorted(versions) == expected_versions
    assert sorted(facts) == expected_facts
    assert expected_versions == sorted([resources.retained_version_id, resources.trigger_version_id])
    assert expected_facts == sorted([resources.retained_confirmed_id, resources.retained_disputed_id])
    with pool.connection() as connection:
        version_count = connection.execute(
            "SELECT count(*) FROM run_document_versions WHERE run_id = %s::uuid",
            (resources.update_run_id,),
        ).fetchone()
        fact_count = connection.execute(
            "SELECT count(*) FROM run_facts WHERE run_id = %s::uuid",
            (resources.update_run_id,),
        ).fetchone()
        relations = connection.execute(
            "SELECT relation FROM run_facts WHERE run_id = %s::uuid",
            (resources.update_run_id,),
        ).fetchall()
    assert version_count == (len(expected_versions),)
    assert fact_count == (len(expected_facts),)
    assert all(row[0] == "CARRIED_FORWARD" for row in relations)


def test_input_drift_after_claim_writes_no_target_memberships(resources: Resources, monkeypatch: pytest.MonkeyPatch) -> None:
    original = preparation_module.IdempotencyRepository.claim

    def claim_then_mutate(repository, *args):
        claimed = original(repository, *args)
        with pool.connection() as other:
            create_fact(other, resources.baseline_run_id, 777, (resources.retained_evidence_id,), FactStatus.CONFIRMED)
            other.commit()
        return claimed

    monkeypatch.setattr(preparation_module.IdempotencyRepository, "claim", claim_then_mutate)
    with pytest.raises(UpdatePreparationInputDriftError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert target_state(resources.update_run_id) == ([], [])
    assert preparation_record(resources.update_run_id).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]


@pytest.mark.parametrize("retryable", [DeadlockDetected, SerializationFailure])
def test_retryable_persistence_failure_retries_atomically(resources: Resources, monkeypatch: pytest.MonkeyPatch, retryable: type[Exception]) -> None:
    original = preparation_module.IdempotencyRepository.mark_completed
    attempts = 0

    def fail_once(repository, *args):
        nonlocal attempts
        attempts += 1
        result = original(repository, *args)
        if attempts == 1:
            raise retryable("retry")
        return result

    monkeypatch.setattr(preparation_module.IdempotencyRepository, "mark_completed", fail_once)
    service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert attempts == 2
    assert set(target_state(resources.update_run_id)[0]) == {resources.retained_version_id, resources.trigger_version_id}


@pytest.mark.parametrize("retryable", [DeadlockDetected, SerializationFailure])
def test_three_retryable_persistence_failures_leave_no_partial_memberships(
    resources: Resources,
    monkeypatch: pytest.MonkeyPatch,
    retryable: type[Exception],
) -> None:
    original = preparation_module.IdempotencyRepository.mark_completed
    attempts = 0

    def fail_every_time(repository, *args):
        nonlocal attempts
        attempts += 1
        original(repository, *args)
        raise retryable("retry")

    monkeypatch.setattr(preparation_module.IdempotencyRepository, "mark_completed", fail_every_time)
    with pytest.raises(retryable):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert attempts == 3
    assert target_state(resources.update_run_id) == ([], [])
    assert preparation_record(resources.update_run_id).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]


def test_genuine_post_staging_database_failure_rolls_back(resources: Resources, monkeypatch: pytest.MonkeyPatch) -> None:
    original = preparation_module.IdempotencyRepository.mark_completed

    def fail_after_completion(repository, *args):
        original(repository, *args)
        repository._connection.execute("SELECT 1 / 0")

    monkeypatch.setattr(preparation_module.IdempotencyRepository, "mark_completed", fail_after_completion)
    with pytest.raises(DivisionByZero):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert target_state(resources.update_run_id) == ([], [])
    assert preparation_record(resources.update_run_id).status is IdempotencyStatus.IN_PROGRESS  # type: ignore[union-attr]


def test_persistence_holds_update_run_lock_until_commit(resources: Resources, monkeypatch: pytest.MonkeyPatch) -> None:
    staged, release = threading.Event(), threading.Event()
    original = preparation_module.RunDocumentVersionRepository.add

    def pause_after_first_scope_write(repository, run_id, document_version_id):
        record = original(repository, run_id, document_version_id)
        staged.set()
        assert release.wait(timeout=5)
        return record

    monkeypatch.setattr(preparation_module.RunDocumentVersionRepository, "add", pause_after_first_scope_write)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service().prepare, PrepareUpdateRunCommand(resources.update_run_id))
        assert staged.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                RunRepository(connection).get_for_update(resources.update_run_id)
            connection.rollback()
        release.set()
        future.result(timeout=5)


def test_baseline_findings_and_review_history_are_not_copied(resources: Resources) -> None:
    with pool.connection() as connection:
        finding = FindingRepository(connection).create(Finding(
            finding_id=str(uuid4()),
            origin_run_id=resources.baseline_run_id,
            finding_type=FindingType.CONTRADICTION,
            title="Historical contradiction",
            severity="MEDIUM",
        ))
        RunFindingRepository(connection).add(
            resources.baseline_run_id, finding.finding_id, RunMembershipRelation.CREATED
        )
        ReviewDecisionRepository(connection).create(
            finding.finding_id,
            ReviewDecisionType.APPROVED,
            "human",
            "reviewer",
            f"historical-{uuid4()}",
        )
        connection.commit()
    service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        target_memberships = connection.execute(
            "SELECT count(*) FROM run_findings WHERE run_id = %s::uuid", (resources.update_run_id,)
        ).fetchone()
        decisions = connection.execute(
            "SELECT count(*) FROM review_decisions WHERE finding_id = %s::uuid", (finding.finding_id,)
        ).fetchone()
    assert target_memberships == (0,)
    assert decisions == (1,)


def test_cross_pile_baseline_is_rejected(resources: Resources) -> None:
    with pool.connection() as connection:
        other = connection.execute("INSERT INTO piles (name) VALUES (%s) RETURNING id", (f"baseline-other-{uuid4()}",)).fetchone()
        assert other is not None
        connection.execute("UPDATE runs SET pile_id = %s::uuid WHERE id = %s::uuid", (str(other[0]), resources.baseline_run_id))
        connection.commit()
    with pytest.raises(UpdatePreparationBaselineError):
        service().prepare(PrepareUpdateRunCommand(resources.update_run_id))
    with pool.connection() as connection:
        connection.execute("UPDATE runs SET pile_id = %s::uuid WHERE id = %s::uuid", (resources.pile_id, resources.baseline_run_id))
        connection.commit()
