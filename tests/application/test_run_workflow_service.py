from dataclasses import dataclass
from threading import Barrier, Event, Thread
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, ForeignKeyViolation, SerializationFailure

from api.application.detect_run_contradictions_service import (
    ContradictionDetectionRunNotEligibleError,
    ContradictionIdempotencyIntegrityError,
    DetectRunContradictionsCommand,
    DetectRunContradictionsService,
)
from api.application.evaluate_run_rules_service import (
    EvaluateRunRulesCommand,
    EvaluateRunRulesService,
    RuleEvaluationClaimLostError,
    RuleEvaluationIdempotencyIntegrityError,
    RuleEvaluationInputDriftError,
    RuleEvaluationRunNotEligibleError,
)
from api.application.extract_run_facts_service import (
    ExtractRunFactsCommand,
    ExtractRunFactsService,
    FactExtractionApplicationError,
    FactExtractionIdempotencyIntegrityError,
    FactExtractionRunNotEligibleError,
)
from api.application.prepare_update_run_service import (
    PrepareUpdateRunCommand,
    PrepareUpdateRunService,
    UpdatePreparationBaselineError,
    UpdatePreparationBaselineScopeError,
)
from api.application.run_rule_scope_service import (
    RuleSetNotFoundError,
    RunRuleScopeConflictError,
    RunRuleScopeService,
)
from api.application.run_workflow_service import (
    RunWorkflowAwaitingUpstream,
    RunWorkflowCommand,
    RunWorkflowGraphIntegrityError,
    RunWorkflowOperationInProgress,
    RunWorkflowOutcome,
    RunWorkflowRuleScopeSelectionRequired,
    RunWorkflowRunFailedError,
    RunWorkflowService,
    RunWorkflowStepEscalatedError,
    RunWorkflowUnmanagedRunError,
)
from db.pool import pool
from domain.contradiction_detection import (
    ContradictionDetectionRequest,
    ContradictionDetectionResponse,
)
from domain.fact import Fact, FactStatus
from domain.fact_extraction import (
    FactEvidenceCandidate,
    FactExtractionCandidate,
    FactExtractionRequest,
    FactExtractionResponse,
    UpdateFactExtractionRequest,
)
from domain.finding import Finding, FindingType
from domain.rule_evaluation import (
    RuleEvaluationOutcome,
    RuleEvaluationRequest,
    RuleEvaluationResponse,
    RuleOutcomeType,
)
from domain.run import RunStage, RunStatus, RunStepStatus
from domain.run_membership import RunMembershipRelation
from domain.run_rule_scope import RunRuleSetSnapshot
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.finding_repository import FindingRepository
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from repositories.run_step_repository import RunStepRepository
from services.contradiction_detection_provider import ContradictionDetectionProviderError
from services.fact_extraction_provider import FactExtractionProviderError
from services.rule_evaluation_provider import RuleEvaluationProviderError


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


def _create_pending_manual_run() -> str:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Workflow service {uuid4()}",),
        ).fetchone()
        assert pile is not None
        run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
            RETURNING id
            """,
            (str(pile[0]),),
        ).fetchone()
        assert run is not None
        connection.commit()
        return str(run[0])


def _create_pending_update_run() -> tuple[str, str]:
    """Create valid fixed lineage without materializing Phase 4A work."""
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Workflow UPDATE {uuid4()}",),
        ).fetchone()
        assert pile is not None
        pile_id = str(pile[0])
        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, completed_at)
            VALUES (%s::uuid, 'MANUAL'::run_type, 'COMPLETED'::run_status, now())
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None
        document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (pile_id, f"workflow-update-{uuid4()}"),
        ).fetchone()
        assert document is not None
        trigger = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            ) VALUES (%s::uuid, 1, %s, %s, 'READY'::document_version_status)
            RETURNING id
            """,
            (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        assert trigger is not None
        update = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, baseline_run_id, trigger_document_version_id)
            VALUES (%s::uuid, 'UPDATE'::run_type, 'PENDING'::run_status, %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(baseline[0]), str(trigger[0])),
        ).fetchone()
        assert update is not None
        connection.commit()
        return str(update[0]), str(trigger[0])


def _complete_identity(scope: str, key: str) -> None:
    with pool.connection() as connection:
        if IdempotencyRepository(connection).get(scope, key) is not None:
            return
        connection.execute(
            """
            INSERT INTO idempotency_records (
                scope, key, status, result_reference, claimed_at, completed_at
            )
            VALUES (%s, %s, 'COMPLETED'::idempotency_status, 'test', clock_timestamp(), now())
            """,
            (scope, key),
        )
        connection.commit()


def _attach_document_chunk(run_id: str) -> None:
    with pool.connection() as connection:
        pile = connection.execute(
            "SELECT pile_id FROM runs WHERE id = %s::uuid",
            (run_id,),
        ).fetchone()
        assert pile is not None
        document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (str(pile[0]), f"workflow-{uuid4()}"),
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
        connection.execute(
            """
            INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
            VALUES (%s::uuid, 0, 'workflow source', %s)
            """,
            (str(version[0]), f"sha256:{uuid4().hex}"),
        )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (run_id, str(version[0])),
        )
        connection.commit()


class _ScopeService:
    def __init__(self) -> None:
        self.calls = 0

    def capture(self, command):
        self.calls += 1
        with pool.connection() as connection:
            repository = RunRuleScopeRepository(connection)
            if repository.get_scope(command.run_id) is None:
                repository.complete_scope(command.run_id)
                connection.commit()


class _CorrectableFailingScopeService(_ScopeService):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def capture(self, command):
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuleSetNotFoundError("missing configured rule set")
        super().capture(command)


class _ReplayScopeService(_ScopeService):
    def __init__(self) -> None:
        super().__init__()
        self._selection: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    def capture(self, command) -> None:
        self.calls += 1
        selection = (command.source_rule_set_ids, command.source_rule_ids)
        if self._selection is not None and selection != self._selection:
            raise RunRuleScopeConflictError("scope selection differs from closed scope")
        self._selection = selection
        with pool.connection() as connection:
            repository = RunRuleScopeRepository(connection)
            if repository.get_scope(command.run_id) is None:
                repository.complete_scope(command.run_id)
                connection.commit()


class _ZeroFactProvider:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, _request) -> FactExtractionResponse:
        self.calls += 1
        return FactExtractionResponse(format="fact-extraction-v1", candidates=())


class _ZeroContradictionProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def detect(self, request):
        self.calls += 1
        self.requests.append(request)
        return ContradictionDetectionResponse(
            format="contradiction-detection-v1",
            contradictions=(),
        )


class _NoViolationRuleProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def evaluate(self, request):
        self.calls += 1
        self.requests.append(request)
        return RuleEvaluationResponse(
            format="rule-evaluation-v1",
            outcomes=tuple(
                RuleEvaluationOutcome(
                    source_rule_id=rule.source_rule_id,
                    outcome=RuleOutcomeType.NO_VIOLATION,
                )
                for rule in request.rules
            ),
        )


@dataclass(frozen=True)
class _RealWorkflowFixture:
    run_id: str
    rule_set_id: str
    rule_id: str


def _create_real_initial_or_manual_run(run_type: str) -> _RealWorkflowFixture:
    """Create only source state; no durable workflow operation is seeded."""
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Workflow real {uuid4()}",),
        ).fetchone()
        assert pile is not None
        pile_id = str(pile[0])
        run = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, %s::run_type, 'PENDING'::run_status)
            RETURNING id
            """,
            (pile_id, run_type),
        ).fetchone()
        assert run is not None
        document = connection.execute(
            "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
            (pile_id, f"workflow-real-{uuid4()}"),
        ).fetchone()
        assert document is not None
        version = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            ) VALUES (%s::uuid, 1, %s, %s, 'READY'::document_version_status)
            RETURNING id
            """,
            (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        assert version is not None
        connection.execute(
            """
            INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
            VALUES (%s::uuid, 0, 'Workflow fact source.', %s)
            """,
            (str(version[0]), f"sha256:{uuid4().hex}"),
        )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (str(run[0]), str(version[0])),
        )
        rule_set = connection.execute(
            """
            INSERT INTO rule_sets (pile_id, name, description, version)
            VALUES (%s::uuid, 'Workflow policy', 'A real captured policy', 1)
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert rule_set is not None
        rule = connection.execute(
            """
            INSERT INTO rules (rule_set_id, rule_key, title, description, severity, definition)
            VALUES (%s::uuid, 'workflow-rule', 'Workflow rule', 'No-op rule', 'MEDIUM', %s::jsonb)
            RETURNING id
            """,
            (str(rule_set[0]), '{"kind":"test"}'),
        ).fetchone()
        assert rule is not None
        connection.commit()
        return _RealWorkflowFixture(str(run[0]), str(rule_set[0]), str(rule[0]))


def _real_workflow(
    extraction_provider: _ZeroFactProvider,
    contradiction_provider: _ZeroContradictionProvider,
    rule_provider: _NoViolationRuleProvider,
) -> RunWorkflowService:
    return RunWorkflowService(
        pool,
        PrepareUpdateRunService(pool),
        RunRuleScopeService(pool),
        ExtractRunFactsService(pool, extraction_provider, "Extract facts only.", "workflow-v1"),
        DetectRunContradictionsService(
            pool,
            contradiction_provider,
            "Detect supplied fact disagreements only.",
            "workflow-v1",
        ),
        EvaluateRunRulesService(pool, rule_provider, "Evaluate captured rules only.", "workflow-v1"),
    )


@dataclass
class _OperationService:
    scope: str
    key_prefix: str
    calls: int = 0

    def _complete(self, run_id: str) -> None:
        self.calls += 1
        _complete_identity(self.scope, f"{self.key_prefix}:{run_id}")

    def prepare(self, command) -> None:
        self._complete(command.run_id)

    def extract(self, command) -> None:
        self._complete(command.run_id)

    def detect(self, command) -> None:
        self._complete(command.run_id)

    def evaluate(self, command) -> None:
        self._complete(command.run_id)


class _ClaimLostRulesService(_OperationService):
    def __init__(self) -> None:
        super().__init__("rule-evaluation-v1", "examine-rules")
        self._fail_once = True

    def evaluate(self, command) -> None:
        if self._fail_once:
            self._fail_once = False
            self.calls += 1
            raise RuleEvaluationClaimLostError("stale rule-evaluation claim")
        self._complete(command.run_id)


def _service() -> tuple[RunWorkflowService, _ScopeService, tuple[_OperationService, ...]]:
    scope = _ScopeService()
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    extraction = _OperationService("fact-extraction-v1", "understand")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    return (
        RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules),
        scope,
        (prepare, extraction, contradiction, rules),
    )


def test_first_call_without_scope_selection_is_write_free() -> None:
    run_id = _create_pending_manual_run()
    service, _, _ = _service()

    with pytest.raises(RunWorkflowRuleScopeSelectionRequired):
        service.advance(RunWorkflowCommand(run_id))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        assert RunRepository(connection).get_by_id(run_id).status is RunStatus.PENDING
        assert RunRuleScopeRepository(connection).get_scope(run_id) is None
        assert IdempotencyRepository(connection).get("fact-extraction-v1", f"understand:{run_id}") is None


def test_clean_pending_run_advances_one_stage_at_a_time_to_review() -> None:
    run_id = _create_pending_manual_run()
    service, scope, operations = _service()
    command = RunWorkflowCommand(run_id, (), ())

    ingest = service.advance(command)
    understand = service.advance(command)
    examine = service.advance(command)
    review = service.advance(command)

    assert ingest.stage is RunStage.INGEST
    assert understand.stage is RunStage.UNDERSTAND
    assert examine.stage is RunStage.EXAMINE
    assert review.stage is RunStage.REVIEW
    assert review.run_status is RunStatus.REVIEW_REQUIRED
    assert scope.calls == 4
    # REVIEW replays each completed provider-backed operation to validate its
    # durable result identity.  The fake represents service calls, not provider
    # calls; real service regressions below pin provider calls separately.
    assert [operation.calls for operation in operations] == [0, 2, 2, 2]
    with pool.connection() as connection:
        steps = RunStepRepository(connection).list_for_run(run_id)
    assert [step.status.value for step in steps] == ["COMPLETED"] * 4
    assert all(step.attempt == 1 for step in steps)


@pytest.mark.parametrize("run_type", ["INITIAL", "MANUAL"])
def test_initial_and_manual_full_workflow_use_real_services(run_type: str) -> None:
    created = _create_real_initial_or_manual_run(run_type)
    extraction = _ZeroFactProvider()
    contradiction = _ZeroContradictionProvider()
    rules = _NoViolationRuleProvider()
    workflow = _real_workflow(extraction, contradiction, rules)
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))

    ingest = workflow.advance(command)
    assert ingest.stage is RunStage.INGEST
    assert ingest.run_status is RunStatus.RUNNING
    with pool.connection() as connection:
        assert RunRuleScopeRepository(connection).get_scope(created.run_id) is not None
        assert IdempotencyRepository(connection).get(
            "fact-extraction-v1", f"understand:{created.run_id}"
        ) is None

    understand = workflow.advance(command)
    examine = workflow.advance(command)
    review = workflow.advance(command)
    assert (understand.stage, examine.stage, review.stage) == (
        RunStage.UNDERSTAND,
        RunStage.EXAMINE,
        RunStage.REVIEW,
    )
    assert review.run_status is RunStatus.REVIEW_REQUIRED
    assert extraction.calls == 1
    assert contradiction.calls == 1
    assert rules.calls == 1
    with pool.connection() as connection:
        steps = RunStepRepository(connection).list_for_run(created.run_id)
        assert [step.stage for step in steps] == [
            RunStage.INGEST,
            RunStage.UNDERSTAND,
            RunStage.EXAMINE,
            RunStage.REVIEW,
        ]
        assert all(step.attempt == 1 and step.status is RunStepStatus.COMPLETED for step in steps)
        assert all(step.completed_at is not None for step in steps)
        for scope, key in (
            ("fact-extraction-v1", f"understand:{created.run_id}"),
            ("contradiction-detection-v1", f"examine-contradictions:{created.run_id}"),
            ("rule-evaluation-v1", f"examine-rules:{created.run_id}"),
        ):
            record = IdempotencyRepository(connection).get(scope, key)
            assert record is not None and record.status.value == "COMPLETED"
            assert record.result_reference is not None and record.result_reference != "test"


def test_pending_run_with_origin_fact_and_no_graph_is_unmanaged() -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        FactRepository(connection).create(
            Fact(
                fact_id=str(uuid4()),
                origin_run_id=run_id,
                subject="subject",
                predicate="predicate",
                object_value="value",
                status=FactStatus.CONFIRMED,
            ),
        )
        connection.commit()

    service, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        service.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        assert len(FactRepository(connection).list_for_origin_run(run_id)) == 1


@pytest.mark.parametrize(
    ("scope", "key_prefix"),
    [
        ("fact-extraction-v1", "understand"),
        ("fact-extraction-update-v2", "understand-update"),
        ("contradiction-detection-v1", "examine-contradictions"),
        ("rule-evaluation-v1", "examine-rules"),
    ],
)
def test_operation_identity_without_graph_is_unmanaged(scope: str, key_prefix: str) -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        IdempotencyRepository(connection).claim(scope, f"{key_prefix}:{run_id}")
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        assert RunRepository(connection).get_by_id(run_id).status is RunStatus.PENDING
        assert IdempotencyRepository(connection).get(scope, f"{key_prefix}:{run_id}") is not None


@pytest.mark.parametrize("signal", ["preparation", "document_scope"])
def test_update_phase4a_state_without_graph_is_unmanaged(signal: str) -> None:
    run_id, trigger_version_id = _create_pending_update_run()
    with pool.connection() as connection:
        if signal == "preparation":
            connection.execute(
                """
                INSERT INTO idempotency_records (scope, key, status, result_reference, claimed_at)
                VALUES ('update-preparation-v1', %s, 'IN_PROGRESS'::idempotency_status, NULL, clock_timestamp())
                """,
                (f"prepare-update:{run_id}",),
            )
        else:
            connection.execute(
                "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
                (run_id, trigger_version_id),
            )
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        run = RunRepository(connection).get_by_id(run_id)
        assert run is not None and run.status is RunStatus.PENDING
        if signal == "preparation":
            assert IdempotencyRepository(connection).get(
                "update-preparation-v1", f"prepare-update:{run_id}"
            ) is not None
        else:
            assert len(connection.execute(
                "SELECT 1 FROM run_document_versions WHERE run_id = %s::uuid", (run_id,)
            ).fetchall()) == 1


@pytest.mark.parametrize("owner", ["fact_membership", "finding_membership"])
def test_foreign_origin_membership_without_graph_is_unmanaged(owner: str) -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        pile = connection.execute(
            "SELECT pile_id FROM runs WHERE id = %s::uuid",
            (run_id,),
        ).fetchone()
        assert pile is not None
        origin = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL'::run_type, 'PENDING'::run_status)
            RETURNING id
            """,
            (str(pile[0]),),
        ).fetchone()
        assert origin is not None
        if owner == "fact_membership":
            fact = FactRepository(connection).create(
                Fact(
                    fact_id=str(uuid4()),
                    origin_run_id=str(origin[0]),
                    subject="subject",
                    predicate="predicate",
                    object_value="value",
                    status=FactStatus.CONFIRMED,
                ),
            )
            RunFactRepository(connection).add(run_id, fact.fact_id, RunMembershipRelation.CREATED)
        else:
            finding = FindingRepository(connection).create(
                Finding(
                    finding_id=str(uuid4()),
                    origin_run_id=str(origin[0]),
                    finding_type=FindingType.CONTRADICTION,
                    title="finding",
                    severity="MEDIUM",
                ),
            )
            RunFindingRepository(connection).add(
                run_id,
                finding.finding_id,
                RunMembershipRelation.CREATED,
            )
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        assert RunRepository(connection).get_by_id(run_id).status is RunStatus.PENDING


def test_finding_origin_without_graph_is_unmanaged_without_membership() -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        FindingRepository(connection).create(
            Finding(
                finding_id=str(uuid4()),
                origin_run_id=run_id,
                finding_type=FindingType.CONTRADICTION,
                title="origin-only finding",
                severity="MEDIUM",
            ),
        )
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        assert RunFindingRepository(connection).list_for_run(run_id) == []
        assert len(FindingRepository(connection).list_for_origin_run(run_id)) == 1


def test_closed_scope_without_graph_is_unmanaged() -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        RunRuleScopeRepository(connection).complete_scope(run_id)
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []


def test_headerless_rule_snapshot_child_cannot_commit() -> None:
    """Migration 002's deferred closure FK rejects an otherwise valid child."""
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        pile = connection.execute(
            "SELECT pile_id FROM runs WHERE id = %s::uuid", (run_id,)
        ).fetchone()
        assert pile is not None
        source = connection.execute(
            """
            INSERT INTO rule_sets (pile_id, name, description, version)
            VALUES (%s::uuid, 'Deferred proof', 'Must not commit without scope', 1)
            RETURNING id
            """,
            (str(pile[0]),),
        ).fetchone()
        assert source is not None
        RunRuleScopeRepository(connection).insert_rule_set_snapshot(
            RunRuleSetSnapshot(
                run_id=run_id,
                source_rule_set_id=str(source[0]),
                name="Deferred proof",
                description="Must not commit without scope",
                version=1,
            )
        )
        with pytest.raises(ForeignKeyViolation):
            connection.commit()
        connection.rollback()
    with pool.connection() as connection:
        assert RunRuleScopeRepository(connection).list_rule_sets_for_run(run_id) == []


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.REVIEW_REQUIRED])
def test_non_pending_run_without_graph_is_unmanaged(status: RunStatus) -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        runs = RunRepository(connection)
        if status is RunStatus.REVIEW_REQUIRED:
            assert runs.update_status(run_id, RunStatus.RUNNING) is not None
        assert runs.update_status(run_id, status) is not None
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowUnmanagedRunError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        assert RunStepRepository(connection).list_for_run(run_id) == []
        current = RunRepository(connection).get_by_id(run_id)
    assert current is not None and current.status is status


def test_partial_graph_fails_closed_without_historical_backfill() -> None:
    run_id = _create_pending_manual_run()
    with pool.connection() as connection:
        RunStepRepository(connection).create(run_id, RunStage.INGEST, 1)
        connection.commit()

    workflow, _, _ = _service()
    with pytest.raises(RunWorkflowGraphIntegrityError):
        workflow.advance(RunWorkflowCommand(run_id, (), ()))

    with pool.connection() as connection:
        steps = RunStepRepository(connection).list_for_run(run_id)
    assert len(steps) == 1
    assert steps[0].status is RunStepStatus.PENDING


def test_existing_graph_reconciles_completed_understand_without_second_call() -> None:
    run_id = _create_pending_manual_run()
    service, _, operations = _service()
    command = RunWorkflowCommand(run_id, (), ())
    service.advance(command)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        understand = repository.get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
        assert understand is not None
        repository.update_execution_state(
            understand.step_id,
            status=RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        _ = IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{run_id}")
        claim = IdempotencyRepository(connection).get("fact-extraction-v1", f"understand:{run_id}")
        assert claim is not None and claim.claimed_at is not None
        IdempotencyRepository(connection).mark_completed(
            "fact-extraction-v1",
            f"understand:{run_id}",
            claim.claim_generation,
            "test",
        )
        connection.commit()

    result = service.advance(command)

    assert result.stage is RunStage.UNDERSTAND
    assert operations[1].calls == 1


def test_completed_understand_replays_real_service_without_second_provider_call() -> None:
    run_id = _create_pending_manual_run()
    _attach_document_chunk(run_id)
    scope = _ScopeService()
    provider = _ZeroFactProvider()
    extraction = ExtractRunFactsService(
        pool,
        provider,
        "Extract grounded facts only.",
        "workflow-replay-v1",
    )
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    workflow = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)
    command = RunWorkflowCommand(run_id, (), ())

    workflow.advance(command)
    extraction.extract(ExtractRunFactsCommand(run_id))
    assert provider.calls == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.UNDERSTAND
    assert result.step.status is RunStepStatus.COMPLETED
    assert result.outcome is RunWorkflowOutcome.REPLAYED
    assert result.advanced is True
    assert provider.calls == 1


def test_review_rejects_a_corrupted_completed_extraction_proof() -> None:
    run_id = _create_pending_manual_run()
    _attach_document_chunk(run_id)
    scope = _ScopeService()
    provider = _ZeroFactProvider()
    extraction = ExtractRunFactsService(
        pool,
        provider,
        "Extract grounded facts only.",
        "workflow-review-proof-v1",
    )
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    workflow = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)
    command = RunWorkflowCommand(run_id, (), ())

    workflow.advance(command)
    workflow.advance(command)
    workflow.advance(command)
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE idempotency_records
            SET result_reference = 'corrupt'
            WHERE scope = 'fact-extraction-v1' AND key = %s
            """,
            (f"understand:{run_id}",),
        )
        connection.commit()

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        review = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.REVIEW, 1)
    assert run is not None and run.status is RunStatus.FAILED
    assert review is not None and review.status is RunStepStatus.FAILED
    assert provider.calls == 1


def test_review_final_lock_rechecks_proofs_after_exact_replay() -> None:
    run_id = _create_pending_manual_run()
    workflow, _, _ = _service()
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    workflow.advance(command)
    workflow.advance(command)

    replayed = Event()
    release = Event()
    errors: list[RunWorkflowGraphIntegrityError] = []
    original = workflow._complete_review

    def pause_after_replay(context) -> None:
        original(context)
        replayed.set()
        assert release.wait(10)

    workflow._complete_review = pause_after_replay  # type: ignore[method-assign]

    def advance_review() -> None:
        try:
            workflow.advance(command)
        except RunWorkflowGraphIntegrityError as error:
            errors.append(error)

    worker = Thread(target=advance_review)
    worker.start()
    assert replayed.wait(10)
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM idempotency_records WHERE scope = 'fact-extraction-v1' AND key = %s",
            (f"understand:{run_id}",),
        )
        connection.commit()
    release.set()
    worker.join(10)

    assert len(errors) == 1
    assert isinstance(errors[0], RunWorkflowGraphIntegrityError)
    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        review = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.REVIEW, 1)
    assert run is not None and run.status is RunStatus.RUNNING
    assert review is not None and review.status is RunStepStatus.RUNNING


def test_rule_claim_loss_reconciles_the_rule_identity_not_contradiction_identity() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    extraction = _OperationService("fact-extraction-v1", "understand")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _ClaimLostRulesService()
    workflow = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)
    command = RunWorkflowCommand(run_id, (), ())

    workflow.advance(command)
    workflow.advance(command)
    _complete_identity("contradiction-detection-v1", f"examine-contradictions:{run_id}")
    _complete_identity("rule-evaluation-v1", f"examine-rules:{run_id}")

    result = workflow.advance(command)

    assert result.stage is RunStage.EXAMINE
    assert result.step.status is RunStepStatus.COMPLETED
    assert rules.calls == 2


def test_matching_closed_scope_selection_replays_before_understand() -> None:
    run_id = _create_pending_manual_run()
    selection = (str(uuid4()),)
    scope = _ReplayScopeService()
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    extraction = _OperationService("fact-extraction-v1", "understand")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    workflow = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)
    command = RunWorkflowCommand(run_id, selection, ())

    workflow.advance(command)
    result = workflow.advance(command)

    assert result.stage is RunStage.UNDERSTAND
    assert scope.calls == 2
    assert extraction.calls == 1


@pytest.mark.parametrize("running", [False, True])
def test_conflicting_closed_scope_selection_prevents_downstream_operation(running: bool) -> None:
    run_id = _create_pending_manual_run()
    initial_selection = (str(uuid4()),)
    conflicting_selection = (str(uuid4()),)
    scope = _ReplayScopeService()
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    extraction = _OperationService("fact-extraction-v1", "understand")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    workflow = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)
    workflow.advance(RunWorkflowCommand(run_id, initial_selection, ()))

    if running:
        with pool.connection() as connection:
            step = RunStepRepository(connection).get_for_run_stage_attempt(
                run_id,
                RunStage.UNDERSTAND,
                1,
            )
            assert step is not None
            RunStepRepository(connection).update_execution_state(
                step.step_id,
                RunStepStatus.RUNNING,
                mark_started=True,
                clear_error=True,
            )
            connection.commit()

    with pytest.raises(RunRuleScopeConflictError):
        workflow.advance(RunWorkflowCommand(run_id, conflicting_selection, ()))

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
    assert run is not None and run.status is RunStatus.RUNNING
    assert step is not None
    assert step.status is (RunStepStatus.RUNNING if running else RunStepStatus.PENDING)
    assert extraction.calls == 0


def test_ingest_rechecks_selection_if_scope_closes_after_preflight() -> None:
    run_id = _create_pending_manual_run()
    selection_a = (str(uuid4()),)
    selection_b = (str(uuid4()),)
    scope = _ReplayScopeService()
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    extraction = _OperationService("fact-extraction-v1", "understand")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    workflow = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)

    with pool.connection() as connection:
        steps = RunStepRepository(connection)
        ingest = steps.create(run_id, RunStage.INGEST, 1)
        for stage in (RunStage.UNDERSTAND, RunStage.EXAMINE, RunStage.REVIEW):
            steps.create(run_id, stage, 1)
        steps.update_execution_state(
            ingest.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        connection.commit()

    preflight_complete = Event()
    continue_ingest = Event()
    errors: list[RunRuleScopeConflictError] = []
    original = workflow._verify_supplied_closed_scope

    def pause_after_preflight(command) -> None:
        original(command)
        preflight_complete.set()
        assert continue_ingest.wait(10)

    workflow._verify_supplied_closed_scope = pause_after_preflight  # type: ignore[method-assign]

    def advance_with_selection_b() -> None:
        try:
            workflow.advance(RunWorkflowCommand(run_id, selection_b, ()))
        except RunRuleScopeConflictError as error:
            errors.append(error)

    worker = Thread(target=advance_with_selection_b)
    worker.start()
    assert preflight_complete.wait(10)
    scope.capture(RunWorkflowCommand(run_id, selection_a, ()))
    continue_ingest.set()
    worker.join(10)

    assert len(errors) == 1
    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        ingest = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.INGEST, 1)
        understand = RunStepRepository(connection).get_for_run_stage_attempt(
            run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert run is not None and run.status is RunStatus.PENDING
    assert ingest is not None and ingest.status is RunStepStatus.RUNNING
    assert understand is not None and understand.status is RunStepStatus.PENDING
    assert scope._selection == (selection_a, ())
    assert extraction.calls == 0


def test_correctable_scope_error_keeps_running_ingest_for_a_later_selection() -> None:
    run_id = _create_pending_manual_run()
    scope = _CorrectableFailingScopeService()
    prepare = _OperationService("update-preparation-v1", "prepare-update")
    extraction = _OperationService("fact-extraction-v1", "understand")
    contradiction = _OperationService("contradiction-detection-v1", "examine-contradictions")
    rules = _OperationService("rule-evaluation-v1", "examine-rules")
    service = RunWorkflowService(pool, prepare, scope, extraction, contradiction, rules)
    command = RunWorkflowCommand(run_id, (), ())

    with pytest.raises(RuleSetNotFoundError):
        service.advance(command)

    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.INGEST, 1)
        assert step is not None and step.status is RunStepStatus.RUNNING
        assert RunRepository(connection).get_by_id(run_id).status is RunStatus.PENDING
        assert RunRuleScopeRepository(connection).get_scope(run_id) is None

    service.advance(command)
    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.INGEST, 1)
        assert step is not None and step.status is RunStepStatus.COMPLETED


def test_in_progress_operation_leaves_running_step_unchanged() -> None:
    run_id = _create_pending_manual_run()
    service, _, _ = _service()
    command = RunWorkflowCommand(run_id, (), ())
    service.advance(command)

    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        understand = repository.get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
        assert understand is not None
        repository.update_execution_state(
            understand.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{run_id}")
        connection.commit()

    with pytest.raises(RunWorkflowOperationInProgress) as raised:
        service.advance(command)

    error = raised.value
    assert (error.run_id, error.stage, error.attempt, error.error_code) == (
        run_id, RunStage.UNDERSTAND, 1, "OPERATION_IN_PROGRESS",
    )
    assert error.operation_scope == "fact-extraction-v1"
    assert error.operation_key == f"understand:{run_id}"
    assert error.claim_generation == 1

    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
        assert step is not None
        assert step.status is RunStepStatus.RUNNING
        assert step.error_code is None


# ---------------------------------------------------------------------------
# Phase 5A final regression-matrix helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _UpdateWorkflowFixture:
    pile_id: str
    baseline_run_id: str
    update_run_id: str
    older_origin_run_id: str
    retained_version_id: str
    retained_chunk_id: str
    retained_evidence_id: str
    replaced_version_id: str
    replaced_chunk_id: str
    trigger_version_id: str
    trigger_chunk_id: str
    carried_fact_id: str
    rule_set_id: str
    rule_id: str


@dataclass(frozen=True)
class _BlockedUpdateFixture:
    pile_id: str
    update_run_id: str
    blocker_run_id: str
    baseline_run_id: str
    trigger_version_id: str
    rule_set_id: str
    rule_id: str


@dataclass(frozen=True)
class _PendingTriggerFixture:
    update_run_id: str
    baseline_run_id: str
    trigger_version_id: str
    rule_set_id: str
    rule_id: str


class _CapturingFactProvider:
    def __init__(
        self,
        *,
        create_trigger_fact: bool = False,
        entered: Event | None = None,
        release: Event | None = None,
        fail: bool = False,
    ) -> None:
        self.create_trigger_fact = create_trigger_fact
        self.entered = entered
        self.release = release
        self.fail = fail
        self.calls: list[FactExtractionRequest | UpdateFactExtractionRequest] = []

    def extract(
        self,
        request: FactExtractionRequest | UpdateFactExtractionRequest,
    ) -> FactExtractionResponse:
        self.calls.append(request)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(10)
        if self.fail:
            raise FactExtractionProviderError("provider unavailable")
        if not self.create_trigger_fact:
            return FactExtractionResponse(format="fact-extraction-v1", candidates=())
        assert len(request.source_chunks) == 1
        chunk = request.source_chunks[0]
        quote = "101 USD"
        start = chunk.content.index(quote)
        return FactExtractionResponse(
            format="fact-extraction-v1",
            candidates=(
                FactExtractionCandidate(
                    subject="trigger amount",
                    predicate="equals",
                    object_value=101,
                    evidence=(
                        FactEvidenceCandidate(
                            chunk_id=chunk.chunk_id,
                            relative_start_offset=start,
                            relative_end_offset=start + len(quote),
                            quoted_text=quote,
                        ),
                    ),
                ),
            ),
        )


class _CapturingContradictionProvider:
    def __init__(self, order: list[str] | None = None, *, fail: bool = False) -> None:
        self.order = order
        self.fail = fail
        self.calls: list[ContradictionDetectionRequest] = []

    def detect(self, request: ContradictionDetectionRequest) -> ContradictionDetectionResponse:
        self.calls.append(request)
        if self.order is not None:
            self.order.append("contradiction")
        if self.fail:
            raise ContradictionDetectionProviderError("provider unavailable")
        return ContradictionDetectionResponse(
            format="contradiction-detection-v1",
            contradictions=(),
        )


class _CapturingRuleProvider:
    def __init__(self, order: list[str] | None = None, *, fail: bool = False) -> None:
        self.order = order
        self.fail = fail
        self.calls: list[RuleEvaluationRequest] = []

    def evaluate(self, request: RuleEvaluationRequest) -> RuleEvaluationResponse:
        self.calls.append(request)
        if self.order is not None:
            self.order.append("rules")
        if self.fail:
            raise RuleEvaluationProviderError("provider unavailable")
        return RuleEvaluationResponse(
            format="rule-evaluation-v1",
            outcomes=tuple(
                RuleEvaluationOutcome(
                    source_rule_id=rule.source_rule_id,
                    outcome=RuleOutcomeType.NO_VIOLATION,
                )
                for rule in request.rules
            ),
        )


class _ObservingScopeService:
    def __init__(self, delegate: RunRuleScopeService, run_id: str) -> None:
        self.delegate = delegate
        self.run_id = run_id
        self.calls = 0
        self.observations: list[tuple[IdempotencyStatus | None, RunStatus]] = []

    def capture(self, command):
        self.calls += 1
        with pool.connection() as connection:
            record = IdempotencyRepository(connection).get(
                "update-preparation-v1",
                f"prepare-update:{self.run_id}",
            )
            run = RunRepository(connection).get_by_id(self.run_id)
            assert run is not None
            self.observations.append((record.status if record is not None else None, run.status))
        return self.delegate.capture(command)


class _CountingPrepareService:
    def __init__(self, delegate: PrepareUpdateRunService) -> None:
        self.delegate = delegate
        self.calls = 0

    def prepare(self, command):
        self.calls += 1
        return self.delegate.prepare(command)


class _RaiseOnceRuleService:
    def __init__(self, delegate: EvaluateRunRulesService, error_type: type[Exception]) -> None:
        self.delegate = delegate
        self.error_type = error_type
        self.calls = 0

    def evaluate(self, command):
        self.calls += 1
        if self.calls == 1:
            raise self.error_type("simulated rule claim-state race")
        return self.delegate.evaluate(command)


class _RaisingPrepareService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def prepare(self, _command):
        raise self.error


class _RaisingExtractionService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def extract(self, _command):
        raise self.error


class _RaisingContradictionService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def detect(self, _command):
        raise self.error


class _RaisingRuleService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def evaluate(self, _command):
        raise self.error


class _LifecycleMismatchExtractionService:
    def extract(self, command):
        _externally_progress_run_to_review(command.run_id)
        raise FactExtractionRunNotEligibleError("run progressed externally")


class _LifecycleMismatchContradictionService:
    def detect(self, command):
        _externally_progress_run_to_review(command.run_id)
        raise ContradictionDetectionRunNotEligibleError("run progressed externally")


class _LifecycleMismatchRuleService:
    def evaluate(self, command):
        _externally_progress_run_to_review(command.run_id)
        raise RuleEvaluationRunNotEligibleError("run progressed externally")


def _externally_progress_run_to_review(run_id: str) -> None:
    with pool.connection() as connection:
        updated = RunRepository(connection).update_status(run_id, RunStatus.REVIEW_REQUIRED)
        assert updated is not None
        connection.commit()


def _create_rule_sources(connection, pile_id: str, prefix: str) -> tuple[str, str]:
    rule_set = connection.execute(
        """
        INSERT INTO rule_sets (pile_id, name, description, version)
        VALUES (%s::uuid, %s, 'Phase 5A captured policy', 1)
        RETURNING id
        """,
        (pile_id, f"{prefix} policy {uuid4()}"),
    ).fetchone()
    assert rule_set is not None
    rule = connection.execute(
        """
        INSERT INTO rules (
            rule_set_id, rule_key, title, description, severity, definition
        )
        VALUES (
            %s::uuid, %s, 'Phase 5A rule', 'No-op regression rule', 'MEDIUM', %s::jsonb
        )
        RETURNING id
        """,
        (str(rule_set[0]), f"{prefix}-{uuid4()}", '{"kind":"phase5a"}'),
    ).fetchone()
    assert rule is not None
    return str(rule_set[0]), str(rule[0])


def _create_document_with_version(
    connection,
    pile_id: str,
    logical_key: str,
    version_number: int,
    content: str,
    status: str = "READY",
) -> tuple[str, str, str]:
    document = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
        (pile_id, f"{logical_key}-{uuid4()}"),
    ).fetchone()
    assert document is not None
    version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        )
        VALUES (%s::uuid, %s, %s, %s, %s::document_version_status)
        RETURNING id
        """,
        (
            str(document[0]),
            version_number,
            f"sha256:{uuid4().hex}",
            f"storage://{uuid4()}",
            status,
        ),
    ).fetchone()
    assert version is not None
    chunk = connection.execute(
        """
        INSERT INTO document_chunks (
            document_version_id, chunk_index, content, content_hash
        )
        VALUES (%s::uuid, 0, %s, %s)
        RETURNING id
        """,
        (str(version[0]), content, f"sha256:{uuid4().hex}"),
    ).fetchone()
    assert chunk is not None
    return str(document[0]), str(version[0]), str(chunk[0])


def _create_version_for_document(
    connection,
    document_id: str,
    version_number: int,
    content: str,
    status: str,
) -> tuple[str, str]:
    version = connection.execute(
        """
        INSERT INTO document_versions (
            document_id, version_number, content_hash, storage_uri, processing_status
        )
        VALUES (%s::uuid, %s, %s, %s, %s::document_version_status)
        RETURNING id
        """,
        (
            document_id,
            version_number,
            f"sha256:{uuid4().hex}",
            f"storage://{uuid4()}",
            status,
        ),
    ).fetchone()
    assert version is not None
    chunk = connection.execute(
        """
        INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
        VALUES (%s::uuid, 0, %s, %s)
        RETURNING id
        """,
        (str(version[0]), content, f"sha256:{uuid4().hex}"),
    ).fetchone()
    assert chunk is not None
    return str(version[0]), str(chunk[0])


def _create_real_update_workflow_fixture() -> _UpdateWorkflowFixture:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Phase5A UPDATE {uuid4()}",),
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
        origin = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL', 'PENDING')
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None and origin is not None
        retained_document_id, retained_version_id, retained_chunk_id = _create_document_with_version(
            connection,
            pile_id,
            "retained",
            1,
            "Retained amount is 100 USD.",
        )
        _ = retained_document_id
        replaced_document_id, replaced_version_id, replaced_chunk_id = _create_document_with_version(
            connection,
            pile_id,
            "replaced",
            1,
            "Old amount is 99 USD.",
        )
        trigger_version_id, trigger_chunk_id = _create_version_for_document(
            connection,
            replaced_document_id,
            2,
            "Changed amount is 101 USD.",
            "READY",
        )
        connection.execute(
            """
            INSERT INTO run_document_versions (run_id, document_version_id)
            VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)
            """,
            (
                str(baseline[0]),
                retained_version_id,
                str(baseline[0]),
                replaced_version_id,
            ),
        )
        retained_evidence = connection.execute(
            """
            INSERT INTO evidence_spans (
                document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash
            )
            VALUES (%s::uuid, %s::uuid, 19, 26, '100 USD', %s)
            RETURNING id
            """,
            (retained_version_id, retained_chunk_id, f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert retained_evidence is not None
        carried = FactRepository(connection).create(
            Fact(
                fact_id=str(uuid4()),
                origin_run_id=str(origin[0]),
                subject="retained amount",
                predicate="equals",
                object_value=100,
                status=FactStatus.CONFIRMED,
            ),
        )
        RunFactRepository(connection).add(
            str(baseline[0]),
            carried.fact_id,
            RunMembershipRelation.CARRIED_FORWARD,
        )
        FactEvidenceRepository(connection).add(carried.fact_id, str(retained_evidence[0]))
        rule_set_id, rule_id = _create_rule_sources(connection, pile_id, "update")
        update = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, baseline_run_id, trigger_document_version_id
            )
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(baseline[0]), trigger_version_id),
        ).fetchone()
        assert update is not None
        connection.commit()
    return _UpdateWorkflowFixture(
        pile_id=pile_id,
        baseline_run_id=str(baseline[0]),
        update_run_id=str(update[0]),
        older_origin_run_id=str(origin[0]),
        retained_version_id=retained_version_id,
        retained_chunk_id=retained_chunk_id,
        retained_evidence_id=str(retained_evidence[0]),
        replaced_version_id=replaced_version_id,
        replaced_chunk_id=replaced_chunk_id,
        trigger_version_id=trigger_version_id,
        trigger_chunk_id=trigger_chunk_id,
        carried_fact_id=carried.fact_id,
        rule_set_id=rule_set_id,
        rule_id=rule_id,
    )


def _create_blocked_update_fixture() -> _BlockedUpdateFixture:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Phase5A blocked {uuid4()}",),
        ).fetchone()
        assert pile is not None
        pile_id = str(pile[0])
        blocker = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL', 'PENDING')
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, completed_at)
            VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now())
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert blocker is not None and baseline is not None
        _document_id, trigger_version_id, _chunk_id = _create_document_with_version(
            connection,
            pile_id,
            "blocked-trigger",
            1,
            "Blocked trigger is 101 USD.",
        )
        rule_set_id, rule_id = _create_rule_sources(connection, pile_id, "blocked")
        update = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, trigger_document_version_id, blocked_on_run_id
            )
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, trigger_version_id, str(blocker[0])),
        ).fetchone()
        assert update is not None
        connection.commit()
    return _BlockedUpdateFixture(
        pile_id=pile_id,
        update_run_id=str(update[0]),
        blocker_run_id=str(blocker[0]),
        baseline_run_id=str(baseline[0]),
        trigger_version_id=trigger_version_id,
        rule_set_id=rule_set_id,
        rule_id=rule_id,
    )


def _create_pending_trigger_fixture(kind: str) -> _PendingTriggerFixture:
    with pool.connection() as connection:
        pile = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"Phase5A trigger {kind} {uuid4()}",),
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
        if kind == "cross_pile":
            _doc, baseline_version_id, _chunk = _create_document_with_version(
                connection,
                pile_id,
                "baseline",
                1,
                "Baseline source.",
            )
            other_pile = connection.execute(
                "INSERT INTO piles (name) VALUES (%s) RETURNING id",
                (f"Cross pile {uuid4()}",),
            ).fetchone()
            assert other_pile is not None
            _trigger_doc, trigger_version_id, _trigger_chunk = _create_document_with_version(
                connection,
                str(other_pile[0]),
                "cross-trigger",
                1,
                "Cross-pile trigger.",
                status="PENDING",
            )
        else:
            document = connection.execute(
                "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
                (pile_id, f"trigger-{kind}-{uuid4()}"),
            ).fetchone()
            assert document is not None
            if kind == "non_advancing":
                trigger_version_id, _trigger_chunk = _create_version_for_document(
                    connection,
                    str(document[0]),
                    2,
                    "Older pending trigger.",
                    "PENDING",
                )
                baseline_version_id, _baseline_chunk = _create_version_for_document(
                    connection,
                    str(document[0]),
                    3,
                    "Newer baseline source.",
                    "READY",
                )
            else:
                baseline_version_id, _baseline_chunk = _create_version_for_document(
                    connection,
                    str(document[0]),
                    1,
                    "Baseline source.",
                    "READY",
                )
                trigger_version_id, _trigger_chunk = _create_version_for_document(
                    connection,
                    str(document[0]),
                    2,
                    "Pending trigger source.",
                    "PENDING",
                )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (str(baseline[0]), baseline_version_id),
        )
        rule_set_id, rule_id = _create_rule_sources(connection, pile_id, "pending-trigger")
        update = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, baseline_run_id, trigger_document_version_id
            )
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(baseline[0]), trigger_version_id),
        ).fetchone()
        assert update is not None
        connection.commit()
    return _PendingTriggerFixture(
        update_run_id=str(update[0]),
        baseline_run_id=str(baseline[0]),
        trigger_version_id=trigger_version_id,
        rule_set_id=rule_set_id,
        rule_id=rule_id,
    )


def _real_services_for_run(
    extraction_provider,
    contradiction_provider,
    rule_provider,
    *,
    prepare_service=None,
    scope_service=None,
) -> RunWorkflowService:
    return RunWorkflowService(
        pool,
        prepare_service or PrepareUpdateRunService(pool),
        scope_service or RunRuleScopeService(pool),
        ExtractRunFactsService(pool, extraction_provider, "Extract facts only.", "phase5a-v1"),
        DetectRunContradictionsService(
            pool,
            contradiction_provider,
            "Detect contradictions only.",
            "phase5a-v1",
        ),
        EvaluateRunRulesService(
            pool,
            rule_provider,
            "Evaluate captured rules only.",
            "phase5a-v1",
        ),
    )


def _real_manual_ready_for_understand(
    extraction_provider=None,
    contradiction_provider=None,
    rule_provider=None,
):
    created = _create_real_initial_or_manual_run("MANUAL")
    extraction_provider = extraction_provider or _CapturingFactProvider()
    contradiction_provider = contradiction_provider or _CapturingContradictionProvider()
    rule_provider = rule_provider or _CapturingRuleProvider()
    workflow = _real_services_for_run(
        extraction_provider,
        contradiction_provider,
        rule_provider,
    )
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)
    return created, workflow, command, extraction_provider, contradiction_provider, rule_provider


def _real_manual_ready_for_examine(
    extraction_provider=None,
    contradiction_provider=None,
    rule_provider=None,
):
    created, workflow, command, extraction, contradiction, rules = _real_manual_ready_for_understand(
        extraction_provider,
        contradiction_provider,
        rule_provider,
    )
    workflow.advance(command)
    return created, workflow, command, extraction, contradiction, rules


def _assert_phase5a_graph_invariants(run_id: str) -> None:
    with pool.connection() as connection:
        steps = RunStepRepository(connection).list_for_run(run_id)
        assert len(steps) == 4
        assert {step.stage for step in steps} == {
            RunStage.INGEST,
            RunStage.UNDERSTAND,
            RunStage.EXAMINE,
            RunStage.REVIEW,
        }
        assert all(step.attempt == 1 for step in steps)
        assert all(step.status is not RunStepStatus.RETRYING for step in steps)
        high_attempt = connection.execute(
            "SELECT count(*) FROM run_steps WHERE run_id = %s::uuid AND attempt > 1",
            (run_id,),
        ).fetchone()
        retry_metrics = connection.execute(
            "SELECT count(*) FROM run_metrics WHERE run_id = %s::uuid AND attempt > 1",
            (run_id,),
        ).fetchone()
    assert high_attempt == (0,)
    assert retry_metrics == (0,)


def _mark_operation_failed(scope: str, key: str) -> None:
    with pool.connection() as connection:
        repository = IdempotencyRepository(connection)
        claimed = repository.claim(scope, key)
        assert claimed is not None and claimed.claimed_at is not None
        failed = repository.mark_failed(scope, key, claimed.claim_generation)
        assert failed is not None
        connection.commit()


def _corrupt_reference(scope: str, key: str) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE idempotency_records
            SET result_reference = 'malformed'
            WHERE scope = %s AND key = %s
            """,
            (scope, key),
        )
        connection.commit()


def _create_four_step_graph_with_running_ingest(run_id: str) -> None:
    with pool.connection() as connection:
        repository = RunStepRepository(connection)
        ingest = repository.create(run_id, RunStage.INGEST, 1)
        repository.create(run_id, RunStage.UNDERSTAND, 1)
        repository.create(run_id, RunStage.EXAMINE, 1)
        repository.create(run_id, RunStage.REVIEW, 1)
        running = repository.update_execution_state(
            ingest.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert running is not None
        connection.commit()


# ---------------------------------------------------------------------------
# Phase 5A final regression matrix
# ---------------------------------------------------------------------------


def test_update_full_workflow_prepares_then_extracts_trigger_only() -> None:
    resources = _create_real_update_workflow_fixture()
    extraction = _CapturingFactProvider(create_trigger_fact=True)
    order: list[str] = []
    contradiction = _CapturingContradictionProvider(order)
    rules = _CapturingRuleProvider(order)
    observing_scope = _ObservingScopeService(
        RunRuleScopeService(pool),
        resources.update_run_id,
    )
    workflow = _real_services_for_run(
        extraction,
        contradiction,
        rules,
        scope_service=observing_scope,
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )

    with pool.connection() as connection:
        before_run = RunRepository(connection).get_by_id(resources.update_run_id)
        before_fact = FactRepository(connection).get_by_id(resources.carried_fact_id)
        before_evidence = FactEvidenceRepository(connection).list_for_fact(resources.carried_fact_id)
    assert before_run is not None and before_run.status is RunStatus.PENDING
    assert before_fact is not None

    ingest = workflow.advance(command)

    assert ingest.stage is RunStage.INGEST
    assert ingest.run_status is RunStatus.RUNNING
    assert observing_scope.observations
    first_scope_observation = observing_scope.observations[0]
    assert first_scope_observation == (IdempotencyStatus.COMPLETED, RunStatus.PENDING)
    with pool.connection() as connection:
        preparation = IdempotencyRepository(connection).get(
            "update-preparation-v1",
            f"prepare-update:{resources.update_run_id}",
        )
        target_versions = {
            item.document_version_id
            for item in RunDocumentVersionRepository(connection).list_for_run(
                resources.update_run_id,
            )
        }
        carried_membership = RunFactRepository(connection).get(
            resources.update_run_id,
            resources.carried_fact_id,
        )
        current_run = RunRepository(connection).get_by_id(resources.update_run_id)
    assert preparation is not None and preparation.status is IdempotencyStatus.COMPLETED
    assert preparation.result_reference is not None
    assert preparation.result_reference.startswith(
        f"update-preparation:{resources.update_run_id}:sha256:",
    )
    assert target_versions == {resources.retained_version_id, resources.trigger_version_id}
    assert carried_membership is not None
    assert carried_membership.relation is RunMembershipRelation.CARRIED_FORWARD
    assert current_run is not None and current_run.status is RunStatus.RUNNING

    understand = workflow.advance(command)

    assert understand.stage is RunStage.UNDERSTAND
    assert len(extraction.calls) == 1
    request = extraction.calls[0]
    assert isinstance(request, UpdateFactExtractionRequest)
    assert request.format == "fact-extraction-update-v2"
    assert request.document_version_ids == (resources.trigger_version_id,)
    assert [chunk.chunk_id for chunk in request.source_chunks] == [resources.trigger_chunk_id]
    assert resources.retained_chunk_id not in {chunk.chunk_id for chunk in request.source_chunks}
    assert resources.replaced_chunk_id not in {chunk.chunk_id for chunk in request.source_chunks}
    with pool.connection() as connection:
        extraction_record = IdempotencyRepository(connection).get(
            "fact-extraction-update-v2",
            f"understand-update:{resources.update_run_id}",
        )
        after_fact = FactRepository(connection).get_by_id(resources.carried_fact_id)
        after_evidence = FactEvidenceRepository(connection).list_for_fact(resources.carried_fact_id)
        memberships = RunFactRepository(connection).list_for_run(resources.update_run_id)
    assert extraction_record is not None
    assert extraction_record.status is IdempotencyStatus.COMPLETED
    assert extraction_record.result_reference is not None
    assert extraction_record.result_reference.startswith(
        f"fact-extraction-update:{resources.update_run_id}:sha256:",
    )
    assert after_fact == before_fact
    assert after_evidence == before_evidence
    created_memberships = [
        item for item in memberships if item.relation is RunMembershipRelation.CREATED
    ]
    assert len(created_memberships) == 1
    created_fact_id = created_memberships[0].fact_id
    assert created_fact_id != resources.carried_fact_id
    assert {
        (item.fact_id, item.relation)
        for item in memberships
    } == {
        (resources.carried_fact_id, RunMembershipRelation.CARRIED_FORWARD),
        (created_fact_id, RunMembershipRelation.CREATED),
    }

    examine = workflow.advance(command)

    assert examine.stage is RunStage.EXAMINE
    assert order == ["contradiction", "rules"]
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1
    expected_fact_ids = {resources.carried_fact_id, created_fact_id}
    assert {fact.fact_id for fact in contradiction.calls[0].source_facts} == expected_fact_ids
    assert {fact.fact_id for fact in rules.calls[0].source_facts} == expected_fact_ids
    with pool.connection() as connection:
        before_review = RunRepository(connection).get_by_id(resources.update_run_id)
        contradiction_record = IdempotencyRepository(connection).get(
            "contradiction-detection-v1",
            f"examine-contradictions:{resources.update_run_id}",
        )
        rule_record = IdempotencyRepository(connection).get(
            "rule-evaluation-v1",
            f"examine-rules:{resources.update_run_id}",
        )
    assert before_review is not None and before_review.status is RunStatus.RUNNING
    assert contradiction_record is not None
    assert contradiction_record.status is IdempotencyStatus.COMPLETED
    assert rule_record is not None and rule_record.status is IdempotencyStatus.COMPLETED

    review = workflow.advance(command)

    assert review.stage is RunStage.REVIEW
    assert review.run_status is RunStatus.REVIEW_REQUIRED
    assert len(extraction.calls) == 1
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1
    _assert_phase5a_graph_invariants(resources.update_run_id)


def test_blocked_update_first_and_repeated_advance_are_stable() -> None:
    resources = _create_blocked_update_fixture()
    prepare = _CountingPrepareService(PrepareUpdateRunService(pool))
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
        prepare_service=prepare,
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )

    with pytest.raises(RunWorkflowAwaitingUpstream) as raised:
        workflow.advance(command)

    error = raised.value
    assert (error.run_id, error.stage, error.attempt, error.error_code) == (
        resources.update_run_id, RunStage.INGEST, 1, "RUN_BLOCKED",
    )
    assert error.operation_scope is None
    assert error.operation_key is None
    assert error.claim_generation is None

    with pool.connection() as connection:
        first_run = RunRepository(connection).get_by_id(resources.update_run_id)
        first_step = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
    assert first_run is not None and first_run.status is RunStatus.PENDING
    assert first_run.blocked_on_run_id == resources.blocker_run_id
    assert first_run.baseline_run_id is None
    assert first_run.parent_run_id is None
    assert first_step is not None
    assert first_step.status is RunStepStatus.AWAITING_UPSTREAM
    assert first_step.started_at is not None
    assert first_step.completed_at is None
    assert first_step.error_code == "RUN_BLOCKED"
    assert first_step.error_message == "waiting on blocked run"
    assert prepare.calls == 0

    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)

    with pool.connection() as connection:
        second_run = RunRepository(connection).get_by_id(resources.update_run_id)
        second_step = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
    assert second_run == first_run
    assert second_step == first_step
    assert prepare.calls == 0
    _assert_phase5a_graph_invariants(resources.update_run_id)


def test_blocked_update_incomplete_unblock_remains_awaiting() -> None:
    resources = _create_blocked_update_fixture()
    prepare = _CountingPrepareService(PrepareUpdateRunService(pool))
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
        prepare_service=prepare,
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)
    with pool.connection() as connection:
        before = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
        connection.execute(
            "UPDATE runs SET blocked_on_run_id = NULL WHERE id = %s::uuid",
            (resources.update_run_id,),
        )
        connection.commit()
    assert before is not None

    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)

    with pool.connection() as connection:
        after = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
        run = RunRepository(connection).get_by_id(resources.update_run_id)
    assert after == before
    assert run is not None
    assert run.blocked_on_run_id is None
    assert run.baseline_run_id is None
    assert run.status is RunStatus.PENDING
    assert prepare.calls == 0


def test_blocked_update_valid_external_resolution_resumes_ingest() -> None:
    resources = _create_blocked_update_fixture()
    prepare = _CountingPrepareService(PrepareUpdateRunService(pool))
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
        prepare_service=prepare,
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)
    with pool.connection() as connection:
        awaiting = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
        assert awaiting is not None
        connection.execute(
            """
            UPDATE runs
            SET blocked_on_run_id = NULL, baseline_run_id = %s::uuid
            WHERE id = %s::uuid
            """,
            (resources.baseline_run_id, resources.update_run_id),
        )
        connection.commit()

    result = workflow.advance(command)

    assert result.stage is RunStage.INGEST
    assert result.step.status is RunStepStatus.COMPLETED
    assert result.run_status is RunStatus.RUNNING
    assert prepare.calls == 1
    assert result.step.started_at == awaiting.started_at
    assert result.step.error_code is None
    assert result.step.error_message is None
    assert result.step.completed_at is not None
    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        preparation = IdempotencyRepository(connection).get(
            "update-preparation-v1",
            f"prepare-update:{resources.update_run_id}",
        )
    assert run is not None
    assert run.baseline_run_id == resources.baseline_run_id
    assert run.blocked_on_run_id is None
    assert run.parent_run_id is None
    assert preparation is not None and preparation.status is IdempotencyStatus.COMPLETED
    _assert_phase5a_graph_invariants(resources.update_run_id)


def test_valid_pending_trigger_awaits_upstream() -> None:
    resources = _create_pending_trigger_fixture("valid")
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )

    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        ingest = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
        preparation = IdempotencyRepository(connection).get(
            "update-preparation-v1",
            f"prepare-update:{resources.update_run_id}",
        )
    assert run is not None and run.status is RunStatus.PENDING
    assert ingest is not None and ingest.status is RunStepStatus.AWAITING_UPSTREAM
    assert ingest.error_code == "TRIGGER_PENDING"
    assert preparation is None
    _assert_phase5a_graph_invariants(resources.update_run_id)


def test_cross_pile_pending_trigger_fails_ingest_and_run() -> None:
    resources = _create_pending_trigger_fixture("cross_pile")
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        ingest = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert ingest is not None and ingest.status is RunStepStatus.FAILED
    assert ingest.status is not RunStepStatus.AWAITING_UPSTREAM


def test_non_advancing_pending_trigger_fails_ingest_and_run() -> None:
    resources = _create_pending_trigger_fixture("non_advancing")
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        ingest = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert ingest is not None and ingest.status is RunStepStatus.FAILED


def test_workflow_observes_real_in_progress_extraction_without_mutation() -> None:
    entered = Event()
    release = Event()
    extraction_provider = _CapturingFactProvider(entered=entered, release=release)
    created = _create_real_initial_or_manual_run("MANUAL")
    extraction_service = ExtractRunFactsService(
        pool,
        extraction_provider,
        "Extract facts only.",
        "phase5a-concurrency-v1",
    )
    workflow = RunWorkflowService(
        pool,
        PrepareUpdateRunService(pool),
        RunRuleScopeService(pool),
        extraction_service,
        DetectRunContradictionsService(
            pool,
            _CapturingContradictionProvider(),
            "Detect contradictions only.",
            "phase5a-v1",
        ),
        EvaluateRunRulesService(
            pool,
            _CapturingRuleProvider(),
            "Evaluate captured rules only.",
            "phase5a-v1",
        ),
    )
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)

    owner_error: list[FactExtractionApplicationError] = []

    def own_claim() -> None:
        try:
            extraction_service.extract(ExtractRunFactsCommand(created.run_id))
        except FactExtractionApplicationError as error:
            owner_error.append(error)

    owner = Thread(target=own_claim)
    owner.start()
    assert entered.wait(10)

    with pytest.raises(RunWorkflowOperationInProgress):
        workflow.advance(command)

    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert step.error_code is None and step.error_message is None
    assert len(extraction_provider.calls) == 1
    release.set()
    owner.join(10)
    assert owner_error == []
    assert len(extraction_provider.calls) == 1


def test_two_workflow_workers_race_real_extraction_claim_once() -> None:
    entered = Event()
    release = Event()
    extraction_provider = _CapturingFactProvider(entered=entered, release=release)
    created = _create_real_initial_or_manual_run("MANUAL")
    contradiction_provider = _CapturingContradictionProvider()
    rule_provider = _CapturingRuleProvider()
    workflow = _real_services_for_run(extraction_provider, contradiction_provider, rule_provider)
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)
    results = []
    errors: list[RunWorkflowOperationInProgress] = []

    def advance_understand() -> None:
        try:
            results.append(workflow.advance(command))
        except RunWorkflowOperationInProgress as error:
            errors.append(error)

    first = Thread(target=advance_understand)
    first.start()
    assert entered.wait(10)
    second = Thread(target=advance_understand)
    second.start()
    second.join(10)
    assert any(isinstance(error, RunWorkflowOperationInProgress) for error in errors)
    assert len(extraction_provider.calls) == 1
    release.set()
    first.join(10)

    assert len(extraction_provider.calls) == 1
    assert len(results) == 1
    assert results[0].stage is RunStage.UNDERSTAND
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{created.run_id}",
        )
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert record is not None and record.status is IdempotencyStatus.COMPLETED
    assert record.result_reference is not None and record.result_reference != "test"
    assert step is not None and step.status is RunStepStatus.COMPLETED
    _assert_phase5a_graph_invariants(created.run_id)


def test_examine_replays_completed_contradiction_then_runs_rules() -> None:
    created, workflow, command, _extraction, contradiction_provider, rule_provider = (
        _real_manual_ready_for_examine()
    )
    contradiction_service = workflow._detect_contradictions
    direct = contradiction_service.detect(DetectRunContradictionsCommand(created.run_id))
    assert direct.replayed is False
    assert len(contradiction_provider.calls) == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.EXAMINE
    assert result.step.status is RunStepStatus.COMPLETED
    assert len(contradiction_provider.calls) == 1
    assert len(rule_provider.calls) == 1


def test_examine_contradiction_in_progress_leaves_step_running() -> None:
    created, workflow, command, _extraction, _contradiction, rule_provider = (
        _real_manual_ready_for_examine()
    )
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "contradiction-detection-v1",
            f"examine-contradictions:{created.run_id}",
        )
        assert claimed is not None
        connection.commit()

    with pytest.raises(RunWorkflowOperationInProgress):
        workflow.advance(command)

    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.EXAMINE,
            1,
        )
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert len(rule_provider.calls) == 0


def test_examine_rules_in_progress_leaves_step_running() -> None:
    created, workflow, command, _extraction, contradiction_provider, _rule_provider = (
        _real_manual_ready_for_examine()
    )
    contradiction_service = workflow._detect_contradictions
    contradiction_service.detect(DetectRunContradictionsCommand(created.run_id))
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "rule-evaluation-v1",
            f"examine-rules:{created.run_id}",
        )
        assert claimed is not None
        connection.commit()

    with pytest.raises(RunWorkflowOperationInProgress):
        workflow.advance(command)

    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.EXAMINE,
            1,
        )
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert len(contradiction_provider.calls) == 1


def _rule_claim_state_reconciliation(error_type: type[Exception]) -> tuple[int, int]:
    created = _create_real_initial_or_manual_run("MANUAL")
    extraction_provider = _CapturingFactProvider()
    contradiction_provider = _CapturingContradictionProvider()
    rule_provider = _CapturingRuleProvider()
    extraction_service = ExtractRunFactsService(
        pool,
        extraction_provider,
        "Extract facts only.",
        "phase5a-v1",
    )
    contradiction_service = DetectRunContradictionsService(
        pool,
        contradiction_provider,
        "Detect contradictions only.",
        "phase5a-v1",
    )
    real_rule_service = EvaluateRunRulesService(
        pool,
        rule_provider,
        "Evaluate captured rules only.",
        "phase5a-v1",
    )
    proxy_rule_service = _RaiseOnceRuleService(real_rule_service, error_type)
    workflow = RunWorkflowService(
        pool,
        PrepareUpdateRunService(pool),
        RunRuleScopeService(pool),
        extraction_service,
        contradiction_service,
        proxy_rule_service,
    )
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)
    workflow.advance(command)
    contradiction_service.detect(DetectRunContradictionsCommand(created.run_id))
    real_rule_service.evaluate(EvaluateRunRulesCommand(created.run_id))
    assert len(rule_provider.calls) == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.EXAMINE
    assert result.step.status is RunStepStatus.COMPLETED
    return proxy_rule_service.calls, len(rule_provider.calls)


def test_examine_rule_claim_loss_reconciles_rule_identity() -> None:
    proxy_calls, provider_calls = _rule_claim_state_reconciliation(RuleEvaluationClaimLostError)
    assert proxy_calls == 2
    assert provider_calls == 1


def test_examine_rule_input_drift_reconciles_rule_identity() -> None:
    proxy_calls, provider_calls = _rule_claim_state_reconciliation(RuleEvaluationInputDriftError)
    assert proxy_calls == 2
    assert provider_calls == 1


def test_examine_rule_claim_loss_replays_completed_identity() -> None:
    proxy_calls, provider_calls = _rule_claim_state_reconciliation(RuleEvaluationClaimLostError)
    assert proxy_calls == 2
    assert provider_calls == 1


def test_examine_failed_contradiction_escalates_step() -> None:
    created, workflow, command, _extraction, _contradiction, rule_provider = (
        _real_manual_ready_for_examine()
    )
    _mark_operation_failed(
        "contradiction-detection-v1",
        f"examine-contradictions:{created.run_id}",
    )

    with pytest.raises(RunWorkflowStepEscalatedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.EXAMINE,
            1,
        )
    assert run is not None and run.status is RunStatus.RUNNING
    assert step is not None and step.status is RunStepStatus.ESCALATED
    assert len(rule_provider.calls) == 0


def test_examine_failed_rule_evaluation_escalates_step() -> None:
    created, workflow, command, _extraction, _contradiction, _rule_provider = (
        _real_manual_ready_for_examine()
    )
    workflow._detect_contradictions.detect(DetectRunContradictionsCommand(created.run_id))
    _mark_operation_failed("rule-evaluation-v1", f"examine-rules:{created.run_id}")

    with pytest.raises(RunWorkflowStepEscalatedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.EXAMINE,
            1,
        )
    assert run is not None and run.status is RunStatus.RUNNING
    assert step is not None and step.status is RunStepStatus.ESCALATED


def test_ingest_replays_real_completed_update_preparation() -> None:
    resources = _create_real_update_workflow_fixture()
    _create_four_step_graph_with_running_ingest(resources.update_run_id)
    preparation_service = PrepareUpdateRunService(pool)
    scope_service = RunRuleScopeService(pool)
    first = preparation_service.prepare(PrepareUpdateRunCommand(resources.update_run_id))
    assert first.replayed is False
    scope_service.capture(
        RunWorkflowCommand(
            resources.update_run_id,
            (resources.rule_set_id,),
            (resources.rule_id,),
        )
    )
    workflow = _real_services_for_run(
        _CapturingFactProvider(create_trigger_fact=True),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
        prepare_service=preparation_service,
        scope_service=scope_service,
    )

    result = workflow.advance(
        RunWorkflowCommand(
            resources.update_run_id,
            (resources.rule_set_id,),
            (resources.rule_id,),
        )
    )

    assert result.stage is RunStage.INGEST
    assert result.step.status is RunStepStatus.COMPLETED
    assert result.run_status is RunStatus.RUNNING
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(
            "update-preparation-v1",
            f"prepare-update:{resources.update_run_id}",
        )
    assert record is not None
    assert record.result_reference == (
        f"update-preparation:{resources.update_run_id}:{first.input_fingerprint}"
    )


def test_understand_replays_real_completed_update_v2_extraction() -> None:
    resources = _create_real_update_workflow_fixture()
    extraction_provider = _CapturingFactProvider(create_trigger_fact=True)
    workflow = _real_services_for_run(
        extraction_provider,
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    workflow.advance(command)
    direct = workflow._extract_facts.extract(ExtractRunFactsCommand(resources.update_run_id))
    assert direct.replayed is False
    assert len(extraction_provider.calls) == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.UNDERSTAND
    assert result.step.status is RunStepStatus.COMPLETED
    assert len(extraction_provider.calls) == 1
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(
            "fact-extraction-update-v2",
            f"understand-update:{resources.update_run_id}",
        )
    assert record is not None
    assert record.result_reference == (
        f"fact-extraction-update:{resources.update_run_id}:{direct.input_fingerprint}"
    )


def test_examine_replays_real_completed_contradiction() -> None:
    created, workflow, command, _extraction, contradiction_provider, rule_provider = (
        _real_manual_ready_for_examine()
    )
    direct = workflow._detect_contradictions.detect(
        DetectRunContradictionsCommand(created.run_id)
    )
    assert direct.replayed is False
    assert len(contradiction_provider.calls) == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.EXAMINE
    assert len(contradiction_provider.calls) == 1
    assert len(rule_provider.calls) == 1


def test_examine_replays_real_completed_rule_evaluation() -> None:
    created, workflow, command, _extraction, contradiction_provider, rule_provider = (
        _real_manual_ready_for_examine()
    )
    workflow._detect_contradictions.detect(DetectRunContradictionsCommand(created.run_id))
    direct = workflow._evaluate_rules.evaluate(EvaluateRunRulesCommand(created.run_id))
    assert direct.replayed is False
    assert len(rule_provider.calls) == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.EXAMINE
    assert len(contradiction_provider.calls) == 1
    assert len(rule_provider.calls) == 1


def test_workflow_rejects_malformed_completed_preparation_reference() -> None:
    resources = _create_real_update_workflow_fixture()
    _create_four_step_graph_with_running_ingest(resources.update_run_id)
    preparation = PrepareUpdateRunService(pool)
    scope = RunRuleScopeService(pool)
    preparation.prepare(PrepareUpdateRunCommand(resources.update_run_id))
    scope.capture(
        RunWorkflowCommand(
            resources.update_run_id,
            (resources.rule_set_id,),
            (resources.rule_id,),
        )
    )
    _corrupt_reference(
        "update-preparation-v1",
        f"prepare-update:{resources.update_run_id}",
    )
    workflow = _real_services_for_run(
        _CapturingFactProvider(create_trigger_fact=True),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
        prepare_service=preparation,
        scope_service=scope,
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(
            RunWorkflowCommand(
                resources.update_run_id,
                (resources.rule_set_id,),
                (resources.rule_id,),
            )
        )

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED


def test_workflow_rejects_malformed_completed_extraction_reference() -> None:
    created, workflow, command, extraction_provider, _contradiction, _rules = (
        _real_manual_ready_for_understand()
    )
    workflow._extract_facts.extract(ExtractRunFactsCommand(created.run_id))
    assert len(extraction_provider.calls) == 1
    _corrupt_reference("fact-extraction-v1", f"understand:{created.run_id}")

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED
    assert len(extraction_provider.calls) == 1


def test_workflow_rejects_malformed_completed_update_extraction_reference() -> None:
    resources = _create_real_update_workflow_fixture()
    extraction_provider = _CapturingFactProvider(create_trigger_fact=True)
    workflow = _real_services_for_run(
        extraction_provider,
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    workflow.advance(command)
    workflow._extract_facts.extract(ExtractRunFactsCommand(resources.update_run_id))
    assert len(extraction_provider.calls) == 1
    _corrupt_reference(
        "fact-extraction-update-v2",
        f"understand-update:{resources.update_run_id}",
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED
    assert len(extraction_provider.calls) == 1


def test_workflow_rejects_malformed_completed_contradiction_reference() -> None:
    created, workflow, command, _extraction, contradiction_provider, rule_provider = (
        _real_manual_ready_for_examine()
    )
    workflow._detect_contradictions.detect(DetectRunContradictionsCommand(created.run_id))
    assert len(contradiction_provider.calls) == 1
    _corrupt_reference(
        "contradiction-detection-v1",
        f"examine-contradictions:{created.run_id}",
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.EXAMINE,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED
    assert len(contradiction_provider.calls) == 1
    assert len(rule_provider.calls) == 0


def test_workflow_rejects_malformed_completed_rule_reference() -> None:
    created, workflow, command, _extraction, contradiction_provider, rule_provider = (
        _real_manual_ready_for_examine()
    )
    workflow._detect_contradictions.detect(DetectRunContradictionsCommand(created.run_id))
    workflow._evaluate_rules.evaluate(EvaluateRunRulesCommand(created.run_id))
    assert len(rule_provider.calls) == 1
    _corrupt_reference("rule-evaluation-v1", f"examine-rules:{created.run_id}")

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.EXAMINE,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED
    assert len(contradiction_provider.calls) == 1
    assert len(rule_provider.calls) == 1


def test_provider_failure_schedules_explicit_retry_without_failing_run() -> None:
    created = _create_real_initial_or_manual_run("MANUAL")
    extraction_provider = _CapturingFactProvider(fail=True)
    workflow = _real_services_for_run(
        extraction_provider,
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)

    result = workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{created.run_id}",
        )
    assert run is not None and run.status is RunStatus.RUNNING
    assert result.outcome.value == "RETRY_SCHEDULED"
    assert step is not None and step.status is RunStepStatus.RETRYING
    assert record is not None and record.status is IdempotencyStatus.FAILED
    assert len(extraction_provider.calls) == 1


def test_pending_retry_successor_starts_and_reopens_only_through_workflow() -> None:
    created = _create_real_initial_or_manual_run("MANUAL")
    extraction_provider = _CapturingFactProvider(fail=True)
    workflow = _real_services_for_run(
        extraction_provider, _CapturingContradictionProvider(), _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)
    scheduled = workflow.advance(command)
    assert scheduled.outcome.value == "RETRY_SCHEDULED"

    extraction_provider.fail = False
    # Crash/restart recovery consults the durable FAILED row and successor
    # provenance; it does not depend on the original workflow instance.
    restarted_workflow = _real_services_for_run(
        extraction_provider,
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    completed = restarted_workflow.advance(command)

    with pool.connection() as connection:
        first = RunStepRepository(connection).get_for_run_stage_attempt(created.run_id, RunStage.UNDERSTAND, 1)
        second = RunStepRepository(connection).get_for_run_stage_attempt(created.run_id, RunStage.UNDERSTAND, 2)
        record = IdempotencyRepository(connection).get("fact-extraction-v1", f"understand:{created.run_id}")
    assert completed.advanced is True
    assert first is not None and first.status is RunStepStatus.RETRYING
    assert first.error_code == "RETRYABLE_PROVIDER_FAILURE"
    assert second is not None and second.status is RunStepStatus.COMPLETED
    assert second.retry_from_claim_generation == 1
    assert record is not None and record.status is IdempotencyStatus.COMPLETED
    assert record.claim_generation == 2
    assert len(extraction_provider.calls) == 2


def test_update_retry_keeps_carried_state_and_persists_only_authoritative_created_fact() -> None:
    resources = _create_real_update_workflow_fixture()
    extraction = _CapturingFactProvider(create_trigger_fact=True, fail=True)
    workflow = _real_services_for_run(
        extraction,
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    workflow.advance(command)
    scheduled = workflow.advance(command)
    assert scheduled.outcome is RunWorkflowOutcome.RETRY_SCHEDULED

    with pool.connection() as connection:
        carried_before = FactRepository(connection).get_by_id(resources.carried_fact_id)
        carried_evidence_before = FactEvidenceRepository(connection).list_for_fact(
            resources.carried_fact_id,
        )
    assert carried_before is not None

    extraction.fail = False
    completed = workflow.advance(command)
    assert completed.outcome is RunWorkflowOutcome.COMPLETED
    with pool.connection() as connection:
        carried_after = FactRepository(connection).get_by_id(resources.carried_fact_id)
        carried_evidence_after = FactEvidenceRepository(connection).list_for_fact(
            resources.carried_fact_id,
        )
        memberships = RunFactRepository(connection).list_for_run(resources.update_run_id)
    assert carried_after == carried_before
    assert carried_evidence_after == carried_evidence_before
    assert {(item.fact_id, item.relation) for item in memberships} == {
        (resources.carried_fact_id, RunMembershipRelation.CARRIED_FORWARD),
        next(
            (item.fact_id, RunMembershipRelation.CREATED)
            for item in memberships
            if item.relation is RunMembershipRelation.CREATED
        ),
    }
    assert len(extraction.calls) == 2


def test_concurrent_retry_scheduling_creates_one_successor_and_one_predecessor_metric() -> None:
    created, workflow, command, extraction, contradiction, rules = _real_manual_ready_for_understand(
        extraction_provider=_CapturingFactProvider(fail=True),
    )
    # The durable provider failure occurs outside the workflow so both workers
    # begin from the same RUNNING attempt plus the same FAILED operation.
    with pytest.raises(FactExtractionApplicationError):
        workflow._extract_facts.extract(ExtractRunFactsCommand(created.run_id))
    extraction.fail = False
    first = _real_services_for_run(extraction, contradiction, rules)
    second = _real_services_for_run(extraction, contradiction, rules)
    barrier = Barrier(2)
    results: list[object] = []
    errors: list[Exception] = []

    def advance(worker: RunWorkflowService) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(worker.advance(command))
        except Exception as error:  # noqa: BLE001 - surface any worker failure in this test
            errors.append(error)

    workers = (Thread(target=advance, args=(first,)), Thread(target=advance, args=(second,)))
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == 2

    with pool.connection() as connection:
        attempts = [
            step
            for step in RunStepRepository(connection).list_for_run(created.run_id)
            if step.stage is RunStage.UNDERSTAND
        ]
        metrics = connection.execute(
            """SELECT count(*) FROM run_metrics
               WHERE run_id = %s::uuid AND stage = 'UNDERSTAND'::run_stage AND attempt = 1""",
            (created.run_id,),
        ).fetchone()
    assert [step.attempt for step in attempts] == [1, 2]
    assert attempts[0].status is RunStepStatus.RETRYING
    assert attempts[1].status is RunStepStatus.PENDING
    assert metrics == (1,)
    assert len(extraction.calls) == 1  # both workers only observed the durable schedule


def test_retry_budget_closes_three_attempts_with_truthful_metrics_and_metadata() -> None:
    """A retryable service failure consumes semantic attempts, never generations."""
    created = _create_real_initial_or_manual_run("MANUAL")
    extraction = _CapturingFactProvider(fail=True)
    workflow = _real_services_for_run(
        extraction, _CapturingContradictionProvider(), _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(created.run_id, (created.rule_set_id,), (created.rule_id,))
    workflow.advance(command)

    first = workflow.advance(command)
    second = workflow.advance(command)
    assert first.outcome.value == "RETRY_SCHEDULED"
    assert second.outcome.value == "RETRY_SCHEDULED"
    assert first.advanced is False and first.successor_attempt == 2
    assert second.advanced is False and second.successor_attempt == 3

    with pytest.raises(RunWorkflowStepEscalatedError) as exhausted:
        workflow.advance(command)
    error = exhausted.value
    assert (error.run_id, error.stage, error.attempt, error.error_code) == (
        created.run_id,
        RunStage.UNDERSTAND,
        3,
        "RETRYABLE_PROVIDER_FAILURE",
    )
    assert error.operation_scope == "fact-extraction-v1"
    assert error.operation_key == f"understand:{created.run_id}"
    assert error.claim_generation == 3

    with pytest.raises(RunWorkflowStepEscalatedError) as restarted:
        workflow.advance(command)
    assert restarted.value.attempt == 3
    assert restarted.value.error_code == "RETRYABLE_PROVIDER_FAILURE"

    with pool.connection() as connection:
        steps = RunStepRepository(connection).list_for_run(created.run_id)
        metrics = connection.execute(
            """SELECT attempt, duration_ms FROM run_metrics
               WHERE run_id = %s::uuid AND stage = 'UNDERSTAND'::run_stage
               ORDER BY attempt""",
            (created.run_id,),
        ).fetchall()
        run = RunRepository(connection).get_by_id(created.run_id)
    understand = [step for step in steps if step.stage is RunStage.UNDERSTAND]
    assert [step.status for step in understand] == [
        RunStepStatus.RETRYING,
        RunStepStatus.RETRYING,
        RunStepStatus.ESCALATED,
    ]
    assert [step.error_code for step in understand] == ["RETRYABLE_PROVIDER_FAILURE"] * 3
    assert len(metrics) == 3 and all(int(metric[1]) >= 0 for metric in metrics)
    assert run is not None and run.status is RunStatus.RUNNING
    assert len(extraction.calls) == 3
    assert not any(step.attempt > 3 for step in steps)


def test_examine_retry_replays_completed_contradiction_and_reopens_only_rules() -> None:
    created, workflow, command, _extraction, contradiction, rules = _real_manual_ready_for_examine()
    workflow._detect_contradictions.detect(DetectRunContradictionsCommand(created.run_id))
    rules.fail = True

    scheduled = workflow.advance(command)
    assert scheduled.outcome.value == "RETRY_SCHEDULED"
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1

    rules.fail = False
    completed = workflow.advance(command)
    assert completed.advanced is True

    with pool.connection() as connection:
        first = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id, RunStage.EXAMINE, 1,
        )
        second = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id, RunStage.EXAMINE, 2,
        )
    assert first is not None and first.status is RunStepStatus.RETRYING
    assert first.retry_operation_scope is None
    assert second is not None and second.status is RunStepStatus.COMPLETED
    assert second.retry_operation_scope == "rule-evaluation-v1"
    assert len(contradiction.calls) == 1  # exact durable replay is provider-free
    assert len(rules.calls) == 2


def test_examine_retry_reopens_contradiction_then_schedules_rules_successor() -> None:
    contradiction = _CapturingContradictionProvider(fail=True)
    rules = _CapturingRuleProvider()
    created, workflow, command, _extraction, contradiction, rules = _real_manual_ready_for_examine(
        extraction_provider=_CapturingFactProvider(),
        contradiction_provider=contradiction,
        rule_provider=rules,
    )

    first = workflow.advance(command)
    assert first.outcome.value == "RETRY_SCHEDULED"
    assert len(contradiction.calls) == 1
    assert rules.calls == []

    contradiction.fail = False
    rules.fail = True
    second = workflow.advance(command)
    assert second.outcome.value == "RETRY_SCHEDULED"

    with pool.connection() as connection:
        attempt_one = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id, RunStage.EXAMINE, 1,
        )
        attempt_two = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id, RunStage.EXAMINE, 2,
        )
        attempt_three = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id, RunStage.EXAMINE, 3,
        )
    assert attempt_one is not None and attempt_one.status is RunStepStatus.RETRYING
    assert attempt_two is not None and attempt_two.status is RunStepStatus.RETRYING
    assert attempt_two.retry_operation_scope == "contradiction-detection-v1"
    assert attempt_three is not None and attempt_three.status is RunStepStatus.PENDING
    assert attempt_three.retry_operation_scope == "rule-evaluation-v1"
    assert len(contradiction.calls) == 2
    assert len(rules.calls) == 1


def test_preexisting_failed_operation_escalates_active_step_without_failing_run() -> None:
    created, workflow, command, extraction_provider, _contradiction, _rules = (
        _real_manual_ready_for_understand()
    )
    _mark_operation_failed("fact-extraction-v1", f"understand:{created.run_id}")

    with pytest.raises(RunWorkflowStepEscalatedError) as raised:
        workflow.advance(command)

    error = raised.value
    assert (error.run_id, error.stage, error.attempt, error.error_code) == (
        created.run_id, RunStage.UNDERSTAND, 1, "OPERATION_FAILED",
    )
    assert error.operation_scope == "fact-extraction-v1"
    assert error.operation_key == f"understand:{created.run_id}"

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert run is not None and run.status is RunStatus.RUNNING
    assert step is not None and step.status is RunStepStatus.ESCALATED
    assert len(extraction_provider.calls) == 0


def _workflow_with_raising_prepare(error: Exception) -> tuple[str, RunWorkflowService, RunWorkflowCommand]:
    run_id, _trigger = _create_pending_update_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _RaisingPrepareService(error),
        scope,
        _OperationService("fact-extraction-update-v2", "understand-update"),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    return run_id, workflow, RunWorkflowCommand(run_id, (), ())


def test_invalid_update_baseline_fails_ingest_and_run() -> None:
    run_id, workflow, command = _workflow_with_raising_prepare(
        UpdatePreparationBaselineError("invalid baseline"),
    )

    with pytest.raises(RunWorkflowRunFailedError) as raised:
        workflow.advance(command)

    error = raised.value
    assert (error.run_id, error.stage, error.attempt, error.error_code) == (
        run_id,
        RunStage.INGEST,
        1,
        "UpdatePreparationBaselineError",
    )
    assert error.operation_scope == "update-preparation-v1"
    assert error.operation_key == f"prepare-update:{run_id}"
    assert error.claim_generation is None

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.INGEST, 1)
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED


def test_invalid_update_baseline_scope_fails_ingest_and_run() -> None:
    run_id, workflow, command = _workflow_with_raising_prepare(
        UpdatePreparationBaselineScopeError("invalid baseline scope"),
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.INGEST, 1)
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED


def test_extraction_integrity_conflict_fails_step_and_run() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _RaisingExtractionService(FactExtractionIdempotencyIntegrityError("corrupt identity")),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED


def test_contradiction_integrity_conflict_fails_step_and_run() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _OperationService("fact-extraction-v1", "understand"),
        _RaisingContradictionService(
            ContradictionIdempotencyIntegrityError("corrupt contradiction identity")
        ),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    workflow.advance(command)

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.EXAMINE, 1)
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED


def test_rule_evaluation_integrity_conflict_fails_step_and_run() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _OperationService("fact-extraction-v1", "understand"),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _RaisingRuleService(RuleEvaluationIdempotencyIntegrityError("corrupt rule identity")),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    workflow.advance(command)

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.EXAMINE, 1)
    assert run is not None and run.status is RunStatus.FAILED
    assert step is not None and step.status is RunStepStatus.FAILED


def test_extraction_run_not_eligible_becomes_graph_integrity_error() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _LifecycleMismatchExtractionService(),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)

    with pytest.raises(RunWorkflowGraphIntegrityError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
    assert run is not None and run.status is RunStatus.REVIEW_REQUIRED
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert step.error_code is None and step.completed_at is None


def test_contradiction_run_not_eligible_becomes_graph_integrity_error() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _OperationService("fact-extraction-v1", "understand"),
        _LifecycleMismatchContradictionService(),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    workflow.advance(command)

    with pytest.raises(RunWorkflowGraphIntegrityError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.EXAMINE, 1)
    assert run is not None and run.status is RunStatus.REVIEW_REQUIRED
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert step.error_code is None and step.completed_at is None


def test_rule_evaluation_run_not_eligible_becomes_graph_integrity_error() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _OperationService("fact-extraction-v1", "understand"),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _LifecycleMismatchRuleService(),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    workflow.advance(command)

    with pytest.raises(RunWorkflowGraphIntegrityError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.EXAMINE, 1)
    assert run is not None and run.status is RunStatus.REVIEW_REQUIRED
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert step.error_code is None and step.completed_at is None


@pytest.mark.parametrize("error_type", [DeadlockDetected, SerializationFailure])
def test_escaped_database_error_leaves_workflow_state_unchanged(error_type: type[Exception]) -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _RaisingExtractionService(error_type("simulated database retry exhaustion")),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)

    with pytest.raises(error_type):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(run_id)
        step = RunStepRepository(connection).get_for_run_stage_attempt(run_id, RunStage.UNDERSTAND, 1)
    assert run is not None and run.status is RunStatus.RUNNING
    assert step is not None and step.status is RunStepStatus.RUNNING
    assert step.completed_at is None
    _assert_phase5a_graph_invariants(run_id)


def test_escaped_deadlock_leaves_workflow_state_unchanged() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _RaisingExtractionService(DeadlockDetected("simulated deadlock")),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    with pytest.raises(DeadlockDetected):
        workflow.advance(command)
    _assert_phase5a_graph_invariants(run_id)


def test_escaped_serialization_failure_leaves_workflow_state_unchanged() -> None:
    run_id = _create_pending_manual_run()
    scope = _ScopeService()
    workflow = RunWorkflowService(
        pool,
        _OperationService("update-preparation-v1", "prepare-update"),
        scope,
        _RaisingExtractionService(SerializationFailure("simulated serialization failure")),
        _OperationService("contradiction-detection-v1", "examine-contradictions"),
        _OperationService("rule-evaluation-v1", "examine-rules"),
    )
    command = RunWorkflowCommand(run_id, (), ())
    workflow.advance(command)
    with pytest.raises(SerializationFailure):
        workflow.advance(command)
    _assert_phase5a_graph_invariants(run_id)


def test_later_advance_reconciles_completed_operation() -> None:
    created, workflow, command, extraction_provider, _contradiction, _rules = (
        _real_manual_ready_for_understand()
    )
    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
        assert step is not None
        running = RunStepRepository(connection).update_execution_state(
            step.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert running is not None
        connection.commit()
    direct = workflow._extract_facts.extract(ExtractRunFactsCommand(created.run_id))
    assert direct.replayed is False
    assert len(extraction_provider.calls) == 1

    result = workflow.advance(command)

    assert result.stage is RunStage.UNDERSTAND
    assert result.step.status is RunStepStatus.COMPLETED
    assert len(extraction_provider.calls) == 1


def test_later_advance_observes_in_progress_operation() -> None:
    created, workflow, command, extraction_provider, _contradiction, _rules = (
        _real_manual_ready_for_understand()
    )
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "fact-extraction-v1",
            f"understand:{created.run_id}",
        )
        assert claimed is not None
        connection.commit()

    with pytest.raises(RunWorkflowOperationInProgress):
        workflow.advance(command)

    assert len(extraction_provider.calls) == 0
    with pool.connection() as connection:
        step = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.UNDERSTAND,
            1,
        )
    assert step is not None and step.status is RunStepStatus.RUNNING


def test_later_advance_invokes_absent_operation() -> None:
    created, workflow, command, extraction_provider, _contradiction, _rules = (
        _real_manual_ready_for_understand()
    )

    result = workflow.advance(command)

    assert result.stage is RunStage.UNDERSTAND
    assert result.step.status is RunStepStatus.COMPLETED
    assert len(extraction_provider.calls) == 1
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{created.run_id}",
        )
    assert record is not None and record.status is IdempotencyStatus.COMPLETED


def _manual_ready_for_review_with_real_services():
    created, workflow, command, extraction, contradiction, rules = _real_manual_ready_for_examine()
    workflow.advance(command)
    with pool.connection() as connection:
        review = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.REVIEW,
            1,
        )
    assert review is not None and review.status is RunStepStatus.PENDING
    return created, workflow, command, extraction, contradiction, rules


def test_review_rejects_malformed_contradiction_proof() -> None:
    created, workflow, command, _extraction, contradiction, rules = (
        _manual_ready_for_review_with_real_services()
    )
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1
    _corrupt_reference(
        "contradiction-detection-v1",
        f"examine-contradictions:{created.run_id}",
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        review = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.REVIEW,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert review is not None and review.status is RunStepStatus.FAILED
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1


def test_review_rejects_malformed_rule_proof() -> None:
    created, workflow, command, _extraction, contradiction, rules = (
        _manual_ready_for_review_with_real_services()
    )
    _corrupt_reference("rule-evaluation-v1", f"examine-rules:{created.run_id}")

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(created.run_id)
        review = RunStepRepository(connection).get_for_run_stage_attempt(
            created.run_id,
            RunStage.REVIEW,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert review is not None and review.status is RunStepStatus.FAILED
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1


def test_review_rejects_malformed_update_preparation_proof() -> None:
    resources = _create_real_update_workflow_fixture()
    extraction = _CapturingFactProvider(create_trigger_fact=True)
    contradiction = _CapturingContradictionProvider()
    rules = _CapturingRuleProvider()
    workflow = _real_services_for_run(extraction, contradiction, rules)
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    workflow.advance(command)
    workflow.advance(command)
    workflow.advance(command)
    _corrupt_reference(
        "update-preparation-v1",
        f"prepare-update:{resources.update_run_id}",
    )

    with pytest.raises(RunWorkflowRunFailedError):
        workflow.advance(command)

    with pool.connection() as connection:
        run = RunRepository(connection).get_by_id(resources.update_run_id)
        review = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.REVIEW,
            1,
        )
    assert run is not None and run.status is RunStatus.FAILED
    assert review is not None and review.status is RunStepStatus.FAILED
    assert len(extraction.calls) == 1
    assert len(contradiction.calls) == 1
    assert len(rules.calls) == 1


def test_workflow_awaiting_resume_preserves_timestamps_and_clears_wait_error() -> None:
    resources = _create_blocked_update_fixture()
    prepare = _CountingPrepareService(PrepareUpdateRunService(pool))
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
        prepare_service=prepare,
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)
    with pool.connection() as connection:
        awaiting = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
        assert awaiting is not None
        connection.execute(
            """
            UPDATE runs
            SET blocked_on_run_id = NULL, baseline_run_id = %s::uuid
            WHERE id = %s::uuid
            """,
            (resources.baseline_run_id, resources.update_run_id),
        )
        connection.commit()

    completed = workflow.advance(command).step

    assert awaiting.started_at is not None
    assert awaiting.completed_at is None
    assert awaiting.error_code == "RUN_BLOCKED"
    assert completed.started_at == awaiting.started_at
    assert completed.completed_at is not None
    assert completed.error_code is None
    assert completed.error_message is None


def test_workflow_ingest_completion_sets_completed_at_once() -> None:
    resources = _create_blocked_update_fixture()
    workflow = _real_services_for_run(
        _CapturingFactProvider(),
        _CapturingContradictionProvider(),
        _CapturingRuleProvider(),
    )
    command = RunWorkflowCommand(
        resources.update_run_id,
        (resources.rule_set_id,),
        (resources.rule_id,),
    )
    with pytest.raises(RunWorkflowAwaitingUpstream):
        workflow.advance(command)
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE runs
            SET blocked_on_run_id = NULL, baseline_run_id = %s::uuid
            WHERE id = %s::uuid
            """,
            (resources.baseline_run_id, resources.update_run_id),
        )
        connection.commit()
    completed = workflow.advance(command).step
    assert completed.completed_at is not None

    with pool.connection() as connection:
        observed = RunStepRepository(connection).get_for_run_stage_attempt(
            resources.update_run_id,
            RunStage.INGEST,
            1,
        )
    assert observed is not None
    assert observed.completed_at == completed.completed_at
    _assert_phase5a_graph_invariants(resources.update_run_id)
