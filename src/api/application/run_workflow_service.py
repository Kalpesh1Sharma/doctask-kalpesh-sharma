"""Durable, PostgreSQL-authoritative sequencing for an approved Run pipeline."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.detect_run_contradictions_service import (
    ContradictionClaimLostError,
    ContradictionDetectionInProgressError,
    ContradictionDetectionPreviouslyFailedError,
    ContradictionDetectionProviderFailure,
    ContradictionDetectionRunNotEligibleError,
    ContradictionIdempotencyIntegrityError,
    ContradictionInputConflictError,
    ContradictionInputDriftError,
    ContradictionInputIntegrityError,
    DetectRunContradictionsCommand,
    DetectRunContradictionsService,
    InvalidContradictionDetectionResponseError,
)
from api.application.evaluate_run_rules_service import (
    EvaluateRunRulesCommand,
    EvaluateRunRulesService,
    InvalidRuleEvaluationResponseError,
    RuleEvaluationClaimLostError,
    RuleEvaluationIdempotencyIntegrityError,
    RuleEvaluationInProgressError,
    RuleEvaluationInputConflictError,
    RuleEvaluationInputDriftError,
    RuleEvaluationInputIntegrityError,
    RuleEvaluationPreviouslyFailedError,
    RuleEvaluationProviderFailure,
    RuleEvaluationRunNotEligibleError,
    RuleScopeNotCapturedError,
    UnsupportedRuleEvaluationError,
)
from api.application.extract_run_facts_service import (
    ExtractRunFactsCommand,
    ExtractRunFactsService,
    FactExtractionClaimLostError,
    FactExtractionIdempotencyIntegrityError,
    FactExtractionInProgressError,
    FactExtractionInputConflictError,
    FactExtractionPreviouslyFailedError,
    FactExtractionProviderFailure,
    FactExtractionRunNotEligibleError,
    InvalidFactExtractionResponseError,
    SourceEvidenceValidationError,
    SourceInputDriftError,
    UpdateFactExtractionPreparationError,
    UpdateFactExtractionTargetStateConflictError,
)
from api.application.prepare_update_run_service import (
    PrepareUpdateRunCommand,
    PrepareUpdateRunService,
    UpdatePreparationApplicationError,
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
from api.application.run_rule_scope_service import (
    CaptureRunRuleScopeCommand,
    InvalidRunRuleScopeSelectionError,
    RuleNotFoundError,
    RuleSelectionMismatchError,
    RuleSetNotFoundError,
    RuleSetPileMismatchError,
    RunRuleScopeConflictError,
    RunRuleScopeRunNotEligibleError,
    RunRuleScopeService,
)
from domain.document_version import DocumentProcessingStatus
from domain.run import RunStage, RunStatus, RunStepStatus, RunType
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.fact_repository import FactRepository
from repositories.finding_repository import FindingRepository
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from repositories.run_step_repository import RunStepRecord, RunStepRepository

_STAGES = (RunStage.INGEST, RunStage.UNDERSTAND, RunStage.EXAMINE, RunStage.REVIEW)
_FACT_SCOPE = "fact-extraction-v1"
_UPDATE_FACT_SCOPE = "fact-extraction-update-v2"
_PREPARATION_SCOPE = "update-preparation-v1"
_CONTRADICTION_SCOPE = "contradiction-detection-v1"
_RULE_SCOPE = "rule-evaluation-v1"


class RunWorkflowError(Exception):
    """Base error for durable workflow sequencing."""


class InvalidRunWorkflowCommandError(RunWorkflowError):
    pass


class RunWorkflowRunNotFoundError(RunWorkflowError):
    pass


class RunWorkflowRuleScopeSelectionRequired(RunWorkflowError):
    pass


class RunWorkflowUnmanagedRunError(RunWorkflowError):
    pass


class RunWorkflowGraphIntegrityError(RunWorkflowError):
    pass


class RunWorkflowOperationInProgress(RunWorkflowError):
    pass


class RunWorkflowStepEscalatedError(RunWorkflowError):
    pass


class RunWorkflowRunFailedError(RunWorkflowError):
    pass


class RunWorkflowAwaitingUpstream(RunWorkflowError):
    pass


@dataclass(frozen=True)
class RunWorkflowCommand:
    run_id: str
    source_rule_set_ids: tuple[str, ...] | None = None
    source_rule_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RunWorkflowResult:
    run_id: str
    stage: RunStage
    step: RunStepRecord
    run_status: RunStatus
    advanced: bool


@dataclass(frozen=True)
class _StageContext:
    run: RunRecord
    step: RunStepRecord


@dataclass(frozen=True)
class _Operation:
    scope: str
    key: str
    invoke: Callable[[], object]


class RunWorkflowService:
    """Advance one durable workflow stage without owning provider work."""

    def __init__(
        self,
        connection_pool: ConnectionPool,
        prepare_update_run_service: PrepareUpdateRunService,
        run_rule_scope_service: RunRuleScopeService,
        extract_run_facts_service: ExtractRunFactsService,
        detect_run_contradictions_service: DetectRunContradictionsService,
        evaluate_run_rules_service: EvaluateRunRulesService,
    ) -> None:
        self._connection_pool = connection_pool
        self._prepare_update = prepare_update_run_service
        self._capture_scope = run_rule_scope_service
        self._extract_facts = extract_run_facts_service
        self._detect_contradictions = detect_run_contradictions_service
        self._evaluate_rules = evaluate_run_rules_service

    def advance(self, command: RunWorkflowCommand) -> RunWorkflowResult:
        normalized = self._normalize_command(command)
        self._verify_supplied_closed_scope(normalized)
        context = self._start_or_reconcile_stage(normalized)
        try:
            if context.step.stage is RunStage.INGEST:
                self._run_ingest(context, normalized)
            elif context.step.stage is RunStage.UNDERSTAND:
                self._run_understand(context)
            elif context.step.stage is RunStage.EXAMINE:
                self._run_examine(context)
            else:
                self._complete_review(context)
        except RunWorkflowError:
            raise
        except (DeadlockDetected, SerializationFailure):
            raise
        except self._correctable_scope_errors():
            raise
        except self._graph_errors():
            self._recheck_not_eligible(context)
            raise
        except UpdatePreparationTriggerError as error:
            self._handle_trigger_error(context, error)
            raise AssertionError("unreachable")
        except self._escalated_errors() as error:
            self._escalate(context, type(error).__name__, str(error))
            raise RunWorkflowStepEscalatedError(str(error)) from error
        except self._permanent_errors() as error:
            self._fail_run(context, type(error).__name__, str(error))
            raise RunWorkflowRunFailedError(str(error)) from error

        return self._complete_stage(context)

    def _verify_supplied_closed_scope(self, command: RunWorkflowCommand) -> None:
        """Replay a caller-supplied selection before downstream step mutation.

        A graphless Run remains subject to the unmanaged-run preflight; this
        method intentionally does not adopt or verify its historical scope.
        """
        if command.source_rule_set_ids is None or command.source_rule_ids is None:
            return
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(command.run_id)
            if run is None:
                raise RunWorkflowRunNotFoundError(f"run {command.run_id} does not exist")
            steps = RunStepRepository(connection).list_for_run(run.run_id)
            if not steps:
                return
            self._validate_graph(steps)
            if RunRuleScopeRepository(connection).get_scope(run.run_id) is None:
                return

        # The Run lock and read-only transaction above are released before
        # calling the scope service, which owns its own locked exact replay.
        self._capture_scope.capture(
            CaptureRunRuleScopeCommand(
                command.run_id,
                command.source_rule_set_ids,
                command.source_rule_ids,
            ),
        )

    def _start_or_reconcile_stage(self, command: RunWorkflowCommand) -> _StageContext:
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(command.run_id)
            if run is None:
                raise RunWorkflowRunNotFoundError(f"run {command.run_id} does not exist")
            steps = RunStepRepository(connection).list_for_run(run.run_id)

            if not steps:
                self._require_fresh_graph(connection, run, command)
                repository = RunStepRepository(connection)
                created = {stage: repository.create(run.run_id, stage, 1) for stage in _STAGES}
                step = self._transition_running(repository, created[RunStage.INGEST])
                if run.run_type is RunType.UPDATE and run.blocked_on_run_id is not None:
                    step = self._await_upstream(repository, step, "RUN_BLOCKED", "waiting on blocked run")
                    connection.commit()
                    raise RunWorkflowAwaitingUpstream(f"run {run.run_id} is blocked upstream")
                connection.commit()
                return _StageContext(run, step)

            self._validate_graph(steps)
            step = self._next_step(steps)
            if step is None:
                raise RunWorkflowGraphIntegrityError("workflow graph has no unfinished stage")
            if step.stage is RunStage.INGEST:
                self._require_scope_selection_if_needed(connection, run, command)
            if step.status is RunStepStatus.AWAITING_UPSTREAM:
                if run.run_type is RunType.UPDATE and run.blocked_on_run_id is not None:
                    raise RunWorkflowAwaitingUpstream(f"run {run.run_id} is blocked upstream")
                if run.run_type is RunType.UPDATE and not self._update_is_ready_to_resume(connection, run):
                    raise RunWorkflowAwaitingUpstream(
                        f"run {run.run_id} has unresolved UPDATE prerequisites",
                    )
                step = self._transition_running(RunStepRepository(connection), step)
                connection.commit()
                return _StageContext(run, step)
            if step.status is RunStepStatus.RUNNING:
                return _StageContext(run, step)
            if step.status in {RunStepStatus.ESCALATED, RunStepStatus.FAILED}:
                raise RunWorkflowStepEscalatedError(f"stage {step.stage.value} is terminal")
            if step.status is not RunStepStatus.PENDING:
                raise RunWorkflowGraphIntegrityError(f"unexpected stage status {step.status.value}")
            step = self._transition_running(RunStepRepository(connection), step)
            connection.commit()
            return _StageContext(run, step)

    def _require_fresh_graph(
        self,
        connection: Connection,
        run: RunRecord,
        command: RunWorkflowCommand,
    ) -> None:
        if run.status is not RunStatus.PENDING:
            raise RunWorkflowUnmanagedRunError("non-pending Run has no Phase 5A graph")
        if self._has_preexisting_stage_work(connection, run):
            raise RunWorkflowUnmanagedRunError("Run has durable work without a Phase 5A graph")
        self._require_scope_selection_if_needed(connection, run, command)

    def _require_scope_selection_if_needed(
        self,
        connection: Connection,
        run: RunRecord,
        command: RunWorkflowCommand,
    ) -> None:
        if RunRuleScopeRepository(connection).get_scope(run.run_id) is not None:
            return
        if command.source_rule_set_ids is None or command.source_rule_ids is None:
            raise RunWorkflowRuleScopeSelectionRequired("rule scope selection is required")

    def _has_preexisting_stage_work(self, connection: Connection, run: RunRecord) -> bool:
        scope = RunRuleScopeRepository(connection)
        if (
            scope.get_scope(run.run_id) is not None
            or scope.list_rule_sets_for_run(run.run_id)
            or scope.list_rules_for_run(run.run_id)
            or RunFactRepository(connection).list_for_run(run.run_id)
            or RunFindingRepository(connection).list_for_run(run.run_id)
            or FactRepository(connection).list_for_origin_run(run.run_id)
            or FindingRepository(connection).list_for_origin_run(run.run_id)
        ):
            return True
        idempotency = IdempotencyRepository(connection)
        identities = (
            (_FACT_SCOPE, f"understand:{run.run_id}"),
            (_UPDATE_FACT_SCOPE, f"understand-update:{run.run_id}"),
            (_CONTRADICTION_SCOPE, f"examine-contradictions:{run.run_id}"),
            (_RULE_SCOPE, f"examine-rules:{run.run_id}"),
        )
        if any(idempotency.get(scope_name, key) is not None for scope_name, key in identities):
            return True
        return run.run_type is RunType.UPDATE and (
            idempotency.get(_PREPARATION_SCOPE, f"prepare-update:{run.run_id}") is not None
            or RunDocumentVersionRepository(connection).list_for_run(run.run_id)
        )

    def _run_ingest(self, context: _StageContext, command: RunWorkflowCommand) -> None:
        if context.run.run_type is RunType.UPDATE:
            self._run_operation(context, self._preparation_operation(context.run))
        self._capture_scope_for_ingest(context, command)

    def _capture_scope_for_ingest(
        self,
        context: _StageContext,
        command: RunWorkflowCommand,
    ) -> None:
        with self._connection_pool.connection() as connection:
            scope = RunRuleScopeRepository(connection).get_scope(context.run.run_id)
        if scope is not None:
            if command.source_rule_set_ids is not None and command.source_rule_ids is not None:
                # Scope closure can race with the earlier pre-stage preflight.
                # Delegate supplied selections to the owning service again so
                # a newly closed scope is exact-replayed or rejected.
                self._capture_scope.capture(
                    CaptureRunRuleScopeCommand(
                        context.run.run_id,
                        command.source_rule_set_ids,
                        command.source_rule_ids,
                    ),
                )
            return
        if command.source_rule_set_ids is None or command.source_rule_ids is None:
            raise RunWorkflowRuleScopeSelectionRequired("rule scope selection is required")
        self._capture_scope.capture(
            CaptureRunRuleScopeCommand(
                context.run.run_id,
                command.source_rule_set_ids,
                command.source_rule_ids,
            ),
        )

    def _run_understand(self, context: _StageContext) -> None:
        self._run_operation(context, self._fact_extraction_operation(context.run))

    def _run_examine(self, context: _StageContext) -> None:
        self._run_operation(context, self._contradiction_operation(context.run))
        self._run_operation(context, self._rule_evaluation_operation(context.run))

    def _run_operation(self, context: _StageContext, operation: _Operation) -> None:
        with self._connection_pool.connection() as connection:
            record = IdempotencyRepository(connection).get(operation.scope, operation.key)
        if record is not None:
            if record.status is IdempotencyStatus.IN_PROGRESS:
                raise RunWorkflowOperationInProgress(f"{operation.scope} is in progress")
            if record.status is IdempotencyStatus.FAILED:
                self._escalate(context, "OPERATION_FAILED", f"{operation.scope} previously failed")
                raise RunWorkflowStepEscalatedError(f"{operation.scope} previously failed")
        try:
            # A completed record is deliberately replayed through the owning
            # service.  Status alone is not a durable semantic proof.
            operation.invoke()
        except self._claim_state_errors() as error:
            self._reconcile_claim_state_error(context, operation, error)

    def _complete_stage(self, context: _StageContext) -> RunWorkflowResult:
        with self._connection_pool.connection() as connection:
            runs = RunRepository(connection)
            run = runs.get_for_update(context.run.run_id)
            if run is None:
                raise RunWorkflowRunNotFoundError(f"run {context.run.run_id} does not exist")
            steps = RunStepRepository(connection)
            step = steps.get_for_run_stage_attempt(run.run_id, context.step.stage, 1)
            if step is None or step.status is not RunStepStatus.RUNNING:
                raise RunWorkflowGraphIntegrityError("active step changed before completion")
            self._verify_stage_proof(connection, run, step.stage)
            if step.stage is RunStage.REVIEW:
                if run.status is not RunStatus.RUNNING:
                    raise RunWorkflowGraphIntegrityError("review requires a running Run")
                run = runs.update_status(run.run_id, RunStatus.REVIEW_REQUIRED)
                assert run is not None
            elif step.stage is RunStage.INGEST:
                if run.status is not RunStatus.PENDING:
                    raise RunWorkflowGraphIntegrityError("ingest requires a pending Run")
                run = runs.update_status(run.run_id, RunStatus.RUNNING)
                assert run is not None
            step = steps.update_execution_state(
                step.step_id,
                RunStepStatus.COMPLETED,
                mark_completed=True,
                clear_error=True,
            )
            assert step is not None
            connection.commit()
            return RunWorkflowResult(run.run_id, step.stage, step, run.status, advanced=True)

    def _complete_review(self, context: _StageContext) -> None:
        # Every upstream service owns exact replay and result-reference
        # validation.  Replaying them here keeps REVIEW truthful after a
        # worker restart without reproducing their fingerprint algorithms.
        if context.run.run_type is RunType.UPDATE:
            self._replay_completed_operation(self._preparation_operation(context.run))
        with self._connection_pool.connection() as connection:
            if RunRuleScopeRepository(connection).get_scope(context.run.run_id) is None:
                raise RunWorkflowGraphIntegrityError("ingest has no closed rule scope")
        self._replay_completed_operation(self._fact_extraction_operation(context.run))
        self._replay_completed_operation(self._contradiction_operation(context.run))
        self._replay_completed_operation(self._rule_evaluation_operation(context.run))

    def _replay_completed_operation(self, operation: _Operation) -> None:
        with self._connection_pool.connection() as connection:
            record = IdempotencyRepository(connection).get(operation.scope, operation.key)
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            raise RunWorkflowGraphIntegrityError(
                f"{operation.scope} has no completed durable proof",
            )
        operation.invoke()

    def _verify_stage_proof(self, connection: Connection, run: RunRecord, stage: RunStage) -> None:
        idempotency = IdempotencyRepository(connection)
        if stage is RunStage.INGEST:
            if run.run_type is RunType.UPDATE:
                self._require_completed(idempotency, _PREPARATION_SCOPE, f"prepare-update:{run.run_id}")
            if RunRuleScopeRepository(connection).get_scope(run.run_id) is None:
                raise RunWorkflowGraphIntegrityError("ingest has no closed rule scope")
        elif stage is RunStage.UNDERSTAND:
            if run.run_type is RunType.UPDATE:
                self._require_completed(idempotency, _UPDATE_FACT_SCOPE, f"understand-update:{run.run_id}")
            else:
                self._require_completed(idempotency, _FACT_SCOPE, f"understand:{run.run_id}")
        elif stage is RunStage.EXAMINE:
            self._require_completed(idempotency, _CONTRADICTION_SCOPE, f"examine-contradictions:{run.run_id}")
            self._require_completed(idempotency, _RULE_SCOPE, f"examine-rules:{run.run_id}")
        else:
            steps = {
                step.stage: step
                for step in RunStepRepository(connection).list_for_run(run.run_id)
            }
            if any(
                steps.get(stage_name) is None
                or steps[stage_name].status is not RunStepStatus.COMPLETED
                for stage_name in (RunStage.INGEST, RunStage.UNDERSTAND, RunStage.EXAMINE)
            ):
                raise RunWorkflowGraphIntegrityError("review has incomplete upstream stage")
            if RunRuleScopeRepository(connection).get_scope(run.run_id) is None:
                raise RunWorkflowGraphIntegrityError("review has no closed rule scope")
            if run.run_type is RunType.UPDATE:
                self._require_completed(
                    idempotency,
                    _PREPARATION_SCOPE,
                    f"prepare-update:{run.run_id}",
                )
            extraction_scope, extraction_key = self._fact_extraction_identity(run)
            self._require_completed(idempotency, extraction_scope, extraction_key)
            self._require_completed(
                idempotency,
                _CONTRADICTION_SCOPE,
                f"examine-contradictions:{run.run_id}",
            )
            self._require_completed(idempotency, _RULE_SCOPE, f"examine-rules:{run.run_id}")

    def _require_completed(self, repository: IdempotencyRepository, scope: str, key: str) -> None:
        record = repository.get(scope, key)
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            raise RunWorkflowGraphIntegrityError(f"{scope} has no completed durable proof")

    def _escalate(self, context: _StageContext, code: str, message: str) -> None:
        self._set_terminal_step(context, RunStepStatus.ESCALATED, code, message, fail_run=False)

    def _fail_run(self, context: _StageContext, code: str, message: str) -> None:
        self._set_terminal_step(context, RunStepStatus.FAILED, code, message, fail_run=True)

    def _set_terminal_step(
        self,
        context: _StageContext,
        status: RunStepStatus,
        code: str,
        message: str,
        *,
        fail_run: bool,
    ) -> None:
        with self._connection_pool.connection() as connection:
            runs = RunRepository(connection)
            run = runs.get_for_update(context.run.run_id)
            if run is None:
                raise RunWorkflowRunNotFoundError(f"run {context.run.run_id} does not exist")
            step = RunStepRepository(connection).get_for_run_stage_attempt(
                run.run_id,
                context.step.stage,
                1,
            )
            if step is None or step.status is not RunStepStatus.RUNNING:
                raise RunWorkflowGraphIntegrityError("active step changed before failure handling")
            updated = RunStepRepository(connection).update_execution_state(
                step.step_id,
                status,
                mark_completed=True,
                error_code=code,
                error_message=message,
            )
            assert updated is not None
            if fail_run:
                updated_run = runs.update_status(run.run_id, RunStatus.FAILED)
                assert updated_run is not None
            connection.commit()

    def _handle_trigger_error(
        self,
        context: _StageContext,
        error: UpdatePreparationTriggerError,
    ) -> None:
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(context.run.run_id)
            if run is None:
                raise RunWorkflowRunNotFoundError(f"run {context.run.run_id} does not exist")
            if self._pending_trigger_is_only_unresolved_prerequisite(connection, run):
                step = RunStepRepository(connection).get_for_run_stage_attempt(
                    run.run_id,
                    context.step.stage,
                    1,
                )
                if step is None or step.status is not RunStepStatus.RUNNING:
                    raise RunWorkflowGraphIntegrityError("active step changed before trigger wait")
                self._await_upstream(
                    RunStepRepository(connection),
                    step,
                    "TRIGGER_PENDING",
                    str(error),
                )
                connection.commit()
                raise RunWorkflowAwaitingUpstream(str(error))
        self._fail_run(context, type(error).__name__, str(error))
        raise RunWorkflowRunFailedError(str(error))

    def _update_is_ready_to_resume(self, connection: Connection, run: RunRecord) -> bool:
        """Return true only when fixed UPDATE prerequisites are now complete.

        This intentionally has no repair behavior: an external owner must
        clear the blocker and establish valid lineage/source state.
        """
        if run.blocked_on_run_id is not None:
            return False
        try:
            from api.application.prepare_update_run_service import (
                build_current_update_preparation_manifest,
            )

            build_current_update_preparation_manifest(connection, run)
        except UpdatePreparationApplicationError:
            return False
        return True

    def _pending_trigger_is_only_unresolved_prerequisite(
        self,
        connection: Connection,
        run: RunRecord,
    ) -> bool:
        if run.baseline_run_id is None or run.trigger_document_version_id is None:
            return False
        baseline = RunRepository(connection).get_by_id(run.baseline_run_id)
        if (
            baseline is None
            or baseline.pile_id != run.pile_id
            or baseline.status is not RunStatus.COMPLETED
        ):
            return False
        trigger = DocumentVersionRepository(connection).get_by_id(
            run.trigger_document_version_id,
        )
        if trigger is None or trigger.processing_status is not DocumentProcessingStatus.PENDING:
            return False
        document = DocumentRepository(connection).get_by_id(trigger.document_id)
        if document is None or document.pile_id != run.pile_id:
            return False
        by_document = {}
        for item in RunDocumentVersionRepository(connection).list_scope_for_run(baseline.run_id):
            if (
                item.document_pile_id != run.pile_id
                or item.processing_status is not DocumentProcessingStatus.READY
                or item.document_id in by_document
            ):
                return False
            by_document[item.document_id] = item
        baseline_version = by_document.get(trigger.document_id)
        return baseline_version is None or (
            baseline_version.document_version_id != trigger.document_version_id
            and trigger.version_number > baseline_version.version_number
        )

    def _reconcile_claim_state_error(
        self,
        context: _StageContext,
        operation: _Operation,
        error: Exception,
    ) -> None:
        with self._connection_pool.connection() as connection:
            record = IdempotencyRepository(connection).get(operation.scope, operation.key)
        if record is not None and record.status is IdempotencyStatus.COMPLETED:
            # The original owner may have committed immediately before this
            # worker observed claim loss or drift.  Validate the exact replay
            # before treating that race as success.
            operation.invoke()
            return
        if record is not None and record.status is IdempotencyStatus.IN_PROGRESS:
            raise RunWorkflowOperationInProgress(str(error))
        if record is not None and record.status is IdempotencyStatus.FAILED:
            self._escalate(context, "OPERATION_FAILED", str(error))
            raise RunWorkflowStepEscalatedError(str(error))
        raise RunWorkflowGraphIntegrityError(str(error))

    def _preparation_operation(self, run: RunRecord) -> _Operation:
        return _Operation(
            _PREPARATION_SCOPE,
            f"prepare-update:{run.run_id}",
            lambda: self._prepare_update.prepare(PrepareUpdateRunCommand(run.run_id)),
        )

    def _fact_extraction_operation(self, run: RunRecord) -> _Operation:
        scope, key = self._fact_extraction_identity(run)
        return _Operation(
            scope,
            key,
            lambda: self._extract_facts.extract(ExtractRunFactsCommand(run.run_id)),
        )

    @staticmethod
    def _fact_extraction_identity(run: RunRecord) -> tuple[str, str]:
        if run.run_type is RunType.UPDATE:
            return _UPDATE_FACT_SCOPE, f"understand-update:{run.run_id}"
        return _FACT_SCOPE, f"understand:{run.run_id}"

    def _contradiction_operation(self, run: RunRecord) -> _Operation:
        return _Operation(
            _CONTRADICTION_SCOPE,
            f"examine-contradictions:{run.run_id}",
            lambda: self._detect_contradictions.detect(DetectRunContradictionsCommand(run.run_id)),
        )

    def _rule_evaluation_operation(self, run: RunRecord) -> _Operation:
        return _Operation(
            _RULE_SCOPE,
            f"examine-rules:{run.run_id}",
            lambda: self._evaluate_rules.evaluate(EvaluateRunRulesCommand(run.run_id)),
        )

    def _recheck_not_eligible(self, context: _StageContext) -> None:
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(context.run.run_id)
            if run is None:
                raise RunWorkflowGraphIntegrityError("Run disappeared during reconciliation")
            steps = RunStepRepository(connection).list_for_run(run.run_id)
            self._validate_graph(steps)
            step = RunStepRepository(connection).get_for_run_stage_attempt(
                run.run_id,
                context.step.stage,
                1,
            )
            expected_status = (
                RunStatus.PENDING if context.step.stage is RunStage.INGEST else RunStatus.RUNNING
            )
            if (
                step is None
                or step.status is not RunStepStatus.RUNNING
                or run.status is not expected_status
            ):
                raise RunWorkflowGraphIntegrityError(
                    "underlying service eligibility conflicts with workflow state",
                )

    def _await_upstream(
        self,
        repository: RunStepRepository,
        step: RunStepRecord,
        code: str,
        message: str,
    ) -> RunStepRecord:
        updated = repository.update_execution_state(
            step.step_id,
            RunStepStatus.AWAITING_UPSTREAM,
            error_code=code,
            error_message=message,
        )
        assert updated is not None
        return updated

    def _transition_running(self, repository: RunStepRepository, step: RunStepRecord) -> RunStepRecord:
        updated = repository.update_execution_state(
            step.step_id,
            RunStepStatus.RUNNING,
            mark_started=True,
            clear_error=True,
        )
        assert updated is not None
        return updated

    def _next_step(self, steps: list[RunStepRecord]) -> RunStepRecord | None:
        by_stage = {step.stage: step for step in steps}
        for stage in _STAGES:
            step = by_stage[stage]
            if step.status is not RunStepStatus.COMPLETED:
                return step
        return None

    def _validate_graph(self, steps: list[RunStepRecord]) -> None:
        if len(steps) != len(_STAGES):
            raise RunWorkflowGraphIntegrityError("workflow graph must contain exactly four steps")
        by_stage = {step.stage: step for step in steps}
        if set(by_stage) != set(_STAGES) or any(step.attempt != 1 for step in steps):
            raise RunWorkflowGraphIntegrityError("workflow graph has unexpected stages or attempts")
        seen_incomplete = False
        for stage in _STAGES:
            step = by_stage[stage]
            if seen_incomplete and step.status is not RunStepStatus.PENDING:
                raise RunWorkflowGraphIntegrityError("downstream stage cannot be active")
            if step.status is not RunStepStatus.COMPLETED:
                seen_incomplete = True

    @staticmethod
    def _correctable_scope_errors():
        return (
            InvalidRunRuleScopeSelectionError,
            RuleSetNotFoundError,
            RuleNotFoundError,
            RuleSetPileMismatchError,
            RuleSelectionMismatchError,
            RunRuleScopeConflictError,
        )

    @staticmethod
    def _graph_errors():
        return (
            UpdatePreparationRunNotEligibleError,
            RunRuleScopeRunNotEligibleError,
            FactExtractionRunNotEligibleError,
            ContradictionDetectionRunNotEligibleError,
            RuleEvaluationRunNotEligibleError,
        )

    @staticmethod
    def _claim_state_errors():
        return (
            UpdatePreparationInProgressError,
            FactExtractionInProgressError,
            ContradictionDetectionInProgressError,
            RuleEvaluationInProgressError,
            UpdatePreparationInputDriftError,
            SourceInputDriftError,
            ContradictionInputDriftError,
            RuleEvaluationInputDriftError,
            UpdatePreparationClaimLostError,
            FactExtractionClaimLostError,
            ContradictionClaimLostError,
            RuleEvaluationClaimLostError,
        )

    @staticmethod
    def _escalated_errors():
        return (
            UpdatePreparationPreviouslyFailedError,
            FactExtractionPreviouslyFailedError,
            ContradictionDetectionPreviouslyFailedError,
            RuleEvaluationPreviouslyFailedError,
            FactExtractionProviderFailure,
            InvalidFactExtractionResponseError,
            SourceEvidenceValidationError,
            ContradictionDetectionProviderFailure,
            InvalidContradictionDetectionResponseError,
            RuleEvaluationProviderFailure,
            InvalidRuleEvaluationResponseError,
            UnsupportedRuleEvaluationError,
        )

    @staticmethod
    def _permanent_errors():
        return (
            UpdatePreparationBaselineError,
            UpdatePreparationBaselineScopeError,
            UpdatePreparationTargetStateConflictError,
            UpdatePreparationInputConflictError,
            UpdatePreparationIdempotencyIntegrityError,
            FactExtractionInputConflictError,
            FactExtractionIdempotencyIntegrityError,
            UpdateFactExtractionPreparationError,
            UpdateFactExtractionTargetStateConflictError,
            ContradictionInputIntegrityError,
            ContradictionInputConflictError,
            ContradictionIdempotencyIntegrityError,
            RuleScopeNotCapturedError,
            RuleEvaluationInputIntegrityError,
            RuleEvaluationInputConflictError,
            RuleEvaluationIdempotencyIntegrityError,
        )

    def _normalize_command(self, command: RunWorkflowCommand) -> RunWorkflowCommand:
        try:
            run_id = str(UUID(command.run_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidRunWorkflowCommandError("run_id must be a valid UUID") from error
        if (command.source_rule_set_ids is None) != (command.source_rule_ids is None):
            raise InvalidRunWorkflowCommandError("rule-set and rule selections must be supplied together")
        if command.source_rule_set_ids is None:
            return RunWorkflowCommand(run_id)
        return RunWorkflowCommand(
            run_id,
            self._normalize_ids(command.source_rule_set_ids, "source_rule_set_ids"),
            self._normalize_ids(command.source_rule_ids, "source_rule_ids"),
        )

    def _normalize_ids(self, values: tuple[str, ...], field: str) -> tuple[str, ...]:
        try:
            normalized = tuple(str(UUID(value)) for value in values)
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidRunWorkflowCommandError(f"{field} must contain valid UUIDs") from error
        if len(set(normalized)) != len(normalized):
            raise InvalidRunWorkflowCommandError(f"{field} cannot contain duplicates")
        return tuple(sorted(normalized))
