"""The sole public fresh-finalization authority for deterministic reports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.finalize_run_service import FinalizeRunService
from api.application.idempotency_claim_takeover import (
    DEFAULT_STALE_CLAIM_AFTER,
    classify_in_progress,
    reclaim_if_stale,
    validate_stale_claim_after,
)
from domain.final_report import FinalReportInput, final_report_input_fingerprint
from domain.render_input import render_input_fingerprint
from domain.run import RunStage, RunStatus, RunStepStatus, RunType
from repositories.audit_event_repository import AuditEventRepository
from repositories.idempotency_repository import (
    IdempotencyFailureKind,
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
    InProgressClaimState,
)
from repositories.output_repository import OutputRecord, OutputRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.run_step_repository import RunStepRecord, RunStepRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from services.final_report_renderer import (
    FinalReportRenderIntegrityError,
    FocusedUpdateProof,
    render_final_report,
    render_focused_update,
)
from services.local_artifact_storage import (
    ArtifactIntegrityError,
    ArtifactPublicationError,
    LocalArtifactStorage,
    StoredArtifact,
)

_SCOPE = "final-report-render-v1"
_KEY_PREFIX = "commit-final-report:"
_REFERENCE = re.compile(
    r"^final-report:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:"
    r"sha256:[0-9a-f]{64}:markdown-v1:sha256:[0-9a-f]{64}$",
)
_MAX_ATTEMPTS = 3
_OUTPUT_TYPE = "FINAL_REPORT"
_AUDIT_ENTITY_TYPE = "output"
_AUDIT_EVENT_TYPE = "FINAL_OUTPUT_COMMITTED"


class CommitFinalReportError(Exception):
    pass


class InvalidCommitFinalReportCommandError(CommitFinalReportError):
    pass


class CommitFinalReportNotFoundError(CommitFinalReportError):
    pass


class CommitFinalReportNotEligibleError(CommitFinalReportError):
    pass


class CommitFinalReportInProgressError(CommitFinalReportError):
    pass


class CommitFinalReportPreviouslyFailedError(CommitFinalReportError):
    pass


class CommitFinalReportIntegrityError(CommitFinalReportError):
    pass


class CommitFinalReportInputDriftError(CommitFinalReportError):
    pass


class CommitFinalReportClaimLostError(CommitFinalReportError):
    pass


class CommitArtifactPublicationError(CommitFinalReportError):
    pass


@dataclass(frozen=True)
class CommitFinalReportCommand:
    run_id: str
    actor_type: str = "system"
    actor_id: str = "commit-final-report"


@dataclass(frozen=True)
class CommitFinalReportResult:
    run_id: str
    output: OutputRecord
    input_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _Claim:
    run: RunRecord
    step: RunStepRecord
    report_input: FinalReportInput
    input_fingerprint: str
    owner_generation: int
    actor_type: str
    actor_id: str
    baseline_output: OutputRecord | None


@dataclass(frozen=True)
class _RenderedReport:
    content: bytes
    content_hash: str
    update_proof: FocusedUpdateProof | None


class CommitFinalReportService:
    def __init__(
        self,
        connection_pool: ConnectionPool,
        artifact_storage: LocalArtifactStorage,
        finalizer: FinalizeRunService,
        *,
        stale_claim_after: timedelta = DEFAULT_STALE_CLAIM_AFTER,
    ) -> None:
        self._connection_pool = connection_pool
        self._artifact_storage = artifact_storage
        self._finalizer = finalizer
        self._stale_claim_after = validate_stale_claim_after(stale_claim_after)

    def commit(self, command: CommitFinalReportCommand) -> CommitFinalReportResult:
        normalized = self._normalize(command)
        outcome = self._claim_or_replay(normalized)
        if isinstance(outcome, CommitFinalReportResult):
            return outcome
        rendered = self._render_claim(outcome)
        try:
            artifact = self._artifact_storage.publish_bytes(rendered.content)
        except (ArtifactPublicationError, OSError) as error:
            self._mark_publication_failed(outcome)
            raise CommitArtifactPublicationError("final report artifact publication failed") from error
        return self._persist(outcome, artifact, rendered)

    def _retry_after_failed_from_workflow(
        self,
        command: CommitFinalReportCommand,
        *,
        expected_failed_generation: int | None,
    ) -> CommitFinalReportResult:
        """Restricted Phase 5C reopen for one retry-successor COMMIT step."""
        if expected_failed_generation is None:
            raise CommitFinalReportIntegrityError("COMMIT retry is missing failed-generation provenance")
        normalized = self._normalize(command)
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(normalized.run_id)
            if run is None:
                raise CommitFinalReportNotFoundError(f"run {normalized.run_id} does not exist")
            context = self._fresh_context(connection, run, normalized)
            reopened = IdempotencyRepository(connection).reopen_failed(
                _SCOPE,
                self._key(run.run_id),
                expected_failed_generation,
                IdempotencyFailureKind.RETRYABLE_ARTIFACT_PUBLICATION,
            )
            if reopened is None:
                return self._resolve_authoritative(
                    connection,
                    run,
                    normalized,
                    IdempotencyRepository(connection),
                )
            connection.commit()
        claim = _Claim(
            context.run, context.step, context.report_input, context.input_fingerprint,
            reopened.claim_generation, normalized.actor_type, normalized.actor_id,
            context.baseline_output,
        )
        rendered = self._render_claim(claim)
        try:
            artifact = self._artifact_storage.publish_bytes(rendered.content)
        except (ArtifactPublicationError, OSError) as error:
            self._mark_publication_failed(claim)
            raise CommitArtifactPublicationError("final report artifact publication failed") from error
        return self._persist(claim, artifact, rendered)

    def _claim_or_replay(self, command: CommitFinalReportCommand) -> _Claim | CommitFinalReportResult:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    value = self._claim_or_replay_once(connection, command)
                    connection.commit()
                    return value
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _claim_or_replay_once(
        self,
        connection: Connection,
        command: CommitFinalReportCommand,
    ) -> _Claim | CommitFinalReportResult:
        run = RunRepository(connection).get_for_update(command.run_id)
        if run is None:
            raise CommitFinalReportNotFoundError(f"run {command.run_id} does not exist")
        key = self._key(run.run_id)
        repository = IdempotencyRepository(connection)
        existing = repository.get(_SCOPE, key)
        existing_output = OutputRepository(connection).get_by_run_and_type(run.run_id, _OUTPUT_TYPE)
        if existing_output is not None and (
            existing is None or existing.status is not IdempotencyStatus.COMPLETED
        ):
            raise CommitFinalReportIntegrityError(
                "FINAL_REPORT output exists without completed COMMIT idempotency proof",
            )
        if existing is None:
            return self._claim_fresh(connection, run, command, repository)
        if existing.status is IdempotencyStatus.COMPLETED:
            return self._replay_completed(connection, run, command, existing)
        if existing.status is IdempotencyStatus.FAILED:
            raise CommitFinalReportPreviouslyFailedError("final report COMMIT previously failed")
        state = classify_in_progress(repository, existing, self._stale_claim_after)
        if state is InProgressClaimState.MATCHING_FRESH:
            raise CommitFinalReportInProgressError("final report COMMIT is already in progress")
        if state is InProgressClaimState.CHANGED_OR_MISSING:
            return self._resolve_authoritative(connection, run, command, repository)
        # Only a stale owner permits rebuilding the review/render input.
        claim = self._fresh_context(connection, run, command)
        replacement = reclaim_if_stale(repository, existing, self._stale_claim_after)
        if replacement is None:
            return self._resolve_authoritative(connection, run, command, repository)
        return _Claim(
            claim.run, claim.step, claim.report_input, claim.input_fingerprint,
            replacement.claim_generation, command.actor_type, command.actor_id,
            claim.baseline_output,
        )

    def _claim_fresh(
        self,
        connection: Connection,
        run: RunRecord,
        command: CommitFinalReportCommand,
        repository: IdempotencyRepository,
    ) -> _Claim | CommitFinalReportResult:
        context = self._fresh_context(connection, run, command)
        claimed = repository.claim(_SCOPE, self._key(run.run_id))
        if claimed is None:
            return self._resolve_authoritative(connection, run, command, repository)
        return _Claim(
            context.run, context.step, context.report_input, context.input_fingerprint,
            claimed.claim_generation, command.actor_type, command.actor_id,
            context.baseline_output,
        )

    def _fresh_context(
        self,
        connection: Connection,
        run: RunRecord,
        command: CommitFinalReportCommand,
    ) -> _Claim:
        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise CommitFinalReportNotEligibleError("COMMIT requires a REVIEW_REQUIRED Run")
        step = self._active_commit_step(connection, run.run_id)
        report_input = self._build_report_input(connection, run)
        baseline_output = self._baseline_output(connection, run)
        return _Claim(
            run,
            step,
            report_input,
            final_report_input_fingerprint(report_input),
            0,
            command.actor_type,
            command.actor_id,
            baseline_output,
        )

    def _active_commit_step(self, connection: Connection, run_id: str) -> RunStepRecord:
        steps = [
            step for step in RunStepRepository(connection).list_for_run(run_id)
            if step.stage is RunStage.COMMIT
        ]
        if not steps:
            raise CommitFinalReportNotEligibleError("Run has no COMMIT workflow step")
        step = max(steps, key=lambda item: item.attempt)
        if step.status is not RunStepStatus.RUNNING:
            raise CommitFinalReportNotEligibleError("COMMIT RunStep is not active")
        return step

    def _build_report_input(self, connection: Connection, run: RunRecord) -> FinalReportInput:
        # Reuse the proven finalizer source builder: this performs the complete
        # review-state and captured-rule-scope validation without the renderer
        # querying mutable tables itself.
        manifest = self._finalizer._build_manifest(connection, run.run_id)
        update_change_set = None
        if run.run_type is RunType.UPDATE:
            self._finalizer._require_update_approval(connection, run, manifest)
            stored = UpdateChangeSetRepository(connection).get_by_run_id(run.run_id)
            if stored is None:
                raise CommitFinalReportIntegrityError("approved UPDATE has no durable change set")
            update_change_set = stored.to_manifest()
        return FinalReportInput(render_input=manifest, update_change_set=update_change_set)

    def _baseline_output(
        self,
        connection: Connection,
        run: RunRecord,
    ) -> OutputRecord | None:
        if run.run_type is not RunType.UPDATE:
            return None
        if run.baseline_run_id is None:
            raise CommitFinalReportIntegrityError("UPDATE Run has no baseline Run")
        baseline_run = RunRepository(connection).get_by_id(run.baseline_run_id)
        if (
            baseline_run is None
            or baseline_run.status is not RunStatus.COMPLETED
            or baseline_run.pile_id != run.pile_id
        ):
            raise CommitFinalReportIntegrityError(
                "UPDATE baseline is not a completed Run in the same pile",
            )
        output = OutputRepository(connection).get_by_run_and_type(
            baseline_run.run_id,
            _OUTPUT_TYPE,
        )
        if output is None:
            raise CommitFinalReportIntegrityError(
                "UPDATE baseline has no FINAL_REPORT artifact to patch",
            )
        return output

    def _render_claim(self, claim: _Claim) -> _RenderedReport:
        if claim.report_input.update_change_set is None:
            content = render_final_report(claim.report_input)
            return _RenderedReport(
                content=content,
                content_hash=f"sha256:{sha256(content).hexdigest()}",
                update_proof=None,
            )
        if claim.baseline_output is None:
            raise CommitFinalReportIntegrityError("UPDATE claim is missing its baseline output")
        try:
            baseline_bytes = self._artifact_storage.read_bytes(
                claim.baseline_output.storage_uri,
                claim.baseline_output.content_hash,
            )
            content, proof = render_focused_update(
                claim.report_input,
                baseline_bytes,
                claim.baseline_output.content_hash,
            )
        except (ArtifactIntegrityError, FinalReportRenderIntegrityError, OSError) as error:
            raise CommitFinalReportIntegrityError(
                "UPDATE baseline artifact cannot produce a focused final report",
            ) from error
        return _RenderedReport(
            content=content,
            content_hash=f"sha256:{sha256(content).hexdigest()}",
            update_proof=proof,
        )

    def _completed_update_proof(
        self,
        connection: Connection,
        run: RunRecord,
        report_input: FinalReportInput,
        output: OutputRecord,
    ) -> tuple[OutputRecord | None, FocusedUpdateProof | None]:
        if report_input.update_change_set is None:
            return None, None
        baseline_output = self._baseline_output(connection, run)
        assert baseline_output is not None
        try:
            baseline_bytes = self._artifact_storage.read_bytes(
                baseline_output.storage_uri,
                baseline_output.content_hash,
            )
            focused_bytes, proof = render_focused_update(
                report_input,
                baseline_bytes,
                baseline_output.content_hash,
            )
        except (ArtifactIntegrityError, FinalReportRenderIntegrityError, OSError) as error:
            raise CommitFinalReportIntegrityError(
                "completed UPDATE cannot verify its focused patch proof",
            ) from error
        expected_hash = f"sha256:{sha256(focused_bytes).hexdigest()}"
        if expected_hash != output.content_hash:
            raise CommitFinalReportIntegrityError(
                "completed UPDATE output is not the focused patch of its baseline",
            )
        return baseline_output, proof

    @staticmethod
    def _require_matching_update_proof(
        report_input: FinalReportInput,
        claimed_baseline_output: OutputRecord | None,
        current_baseline_output: OutputRecord | None,
        update_proof: FocusedUpdateProof | None,
    ) -> None:
        if report_input.update_change_set is None:
            if (
                claimed_baseline_output is not None
                or current_baseline_output is not None
                or update_proof is not None
            ):
                raise CommitFinalReportIntegrityError(
                    "non-UPDATE COMMIT cannot carry focused patch state",
                )
            return
        if (
            claimed_baseline_output is None
            or current_baseline_output is None
            or update_proof is None
        ):
            raise CommitFinalReportIntegrityError("UPDATE COMMIT is missing focused patch state")
        if claimed_baseline_output != current_baseline_output:
            raise CommitFinalReportInputDriftError(
                "UPDATE baseline output changed after COMMIT claim",
            )
        if current_baseline_output.run_id != report_input.update_change_set.baseline_run_id:
            raise CommitFinalReportIntegrityError(
                "focused patch baseline output does not belong to the approved baseline Run",
            )
        if update_proof.baseline_content_hash != current_baseline_output.content_hash:
            raise CommitFinalReportIntegrityError(
                "focused patch proof is bound to a different baseline artifact",
            )

    def _replay_completed(
        self,
        connection: Connection,
        run: RunRecord,
        command: CommitFinalReportCommand,
        record: IdempotencyRecord,
    ) -> CommitFinalReportResult:
        if record.result_reference is None or not _REFERENCE.fullmatch(record.result_reference):
            raise CommitFinalReportIntegrityError("completed COMMIT has a malformed result reference")
        report_input = self._build_report_input(connection, run)
        fingerprint = final_report_input_fingerprint(report_input)
        output = OutputRepository(connection).get_by_run_and_type(run.run_id, "FINAL_REPORT")
        if output is None:
            raise CommitFinalReportIntegrityError("completed COMMIT has no FINAL_REPORT output")
        expected = self._reference(run.run_id, fingerprint, output.content_hash)
        if record.result_reference != expected:
            raise CommitFinalReportInputDriftError("completed COMMIT has a different report input")
        steps = [
            step
            for step in RunStepRepository(connection).list_for_run(run.run_id)
            if step.stage is RunStage.COMMIT
        ]
        if (
            run.status is not RunStatus.COMPLETED
            or not steps
            or max(steps, key=lambda item: item.attempt).status is not RunStepStatus.COMPLETED
        ):
            raise CommitFinalReportIntegrityError("completed COMMIT is missing its Run/RunStep proof")
        baseline_output, update_proof = self._completed_update_proof(
            connection,
            run,
            report_input,
            output,
        )
        self._require_completed_audit_binding(
            connection,
            run,
            report_input,
            output,
            baseline_output,
            update_proof,
        )
        try:
            self._artifact_storage.verify(output.storage_uri, output.content_hash)
        except Exception as error:
            raise CommitFinalReportIntegrityError("completed COMMIT artifact cannot be verified") from error
        return CommitFinalReportResult(run.run_id, output, fingerprint, replayed=True)

    def _require_completed_audit_binding(
        self,
        connection: Connection,
        run: RunRecord,
        report_input: FinalReportInput,
        output: OutputRecord,
        baseline_output: OutputRecord | None,
        update_proof: FocusedUpdateProof | None,
    ) -> None:
        bindings = [
            event
            for event in AuditEventRepository(connection).list_for_run(run.run_id)
            if event.entity_type == _AUDIT_ENTITY_TYPE
            and event.entity_id == output.output_id
            and event.event_type == _AUDIT_EVENT_TYPE
        ]
        if len(bindings) != 1:
            raise CommitFinalReportIntegrityError(
                "completed COMMIT does not have exactly one final-output audit binding",
            )
        expected_payload = self._audit_payload(
            run.run_id,
            render_input_fingerprint(report_input.render_input),
            output,
            report_input,
            baseline_output,
            update_proof,
        )
        if bindings[0].payload != expected_payload:
            raise CommitFinalReportIntegrityError(
                "completed COMMIT final-output audit binding is malformed",
            )

    def _resolve_authoritative(
        self,
        connection: Connection,
        run: RunRecord,
        command: CommitFinalReportCommand,
        repository: IdempotencyRepository,
    ) -> CommitFinalReportResult:
        current = repository.get(_SCOPE, self._key(run.run_id))
        if current is None:
            raise CommitFinalReportIntegrityError("COMMIT idempotency record disappeared")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise CommitFinalReportInProgressError("final report COMMIT is already in progress")
        if current.status is IdempotencyStatus.FAILED:
            raise CommitFinalReportPreviouslyFailedError("final report COMMIT previously failed")
        return self._replay_completed(connection, run, command, current)

    def _persist(
        self,
        claim: _Claim,
        artifact: StoredArtifact,
        rendered: _RenderedReport | None = None,
    ) -> CommitFinalReportResult:
        """Persist one fresh completion under an authoritative re-check.

        This is intentionally private: :meth:`commit` is the sole public fresh
        finalization operation.  Every retry reconstructs current durable input
        and proves the published bytes are exactly the deterministic report for
        that input before any completion state can commit.
        """
        authoritative_render = rendered if rendered is not None else self._render_claim(claim)
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    result = self._persist_once(connection, claim, artifact, authoritative_render)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _persist_once(
        self,
        connection: Connection,
        claim: _Claim,
        artifact: StoredArtifact,
        rendered: _RenderedReport,
    ) -> CommitFinalReportResult:
        run = RunRepository(connection).get_for_update(claim.run.run_id)
        if run is None:
            raise CommitFinalReportIntegrityError("claimed Run disappeared before finalization")
        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise CommitFinalReportIntegrityError("fresh COMMIT no longer owns a REVIEW_REQUIRED Run")

        output_repository = OutputRepository(connection)
        if output_repository.get_by_run_and_type(run.run_id, _OUTPUT_TYPE) is not None:
            raise CommitFinalReportIntegrityError("fresh COMMIT found an existing final output")

        step = RunStepRepository(connection).get_by_id(claim.step.step_id)
        if (
            step is None
            or step.run_id != run.run_id
            or step.stage is not RunStage.COMMIT
            or step.attempt != claim.step.attempt
            or step.status is not RunStepStatus.RUNNING
        ):
            raise CommitFinalReportIntegrityError("COMMIT execution context is not active for this Run")

        idempotency_repository = IdempotencyRepository(connection)
        key = self._key(run.run_id)
        current_claim = idempotency_repository.get(_SCOPE, key)
        if (
            current_claim is None
            or current_claim.status is not IdempotencyStatus.IN_PROGRESS
            or current_claim.claim_generation != claim.owner_generation
        ):
            raise CommitFinalReportClaimLostError("COMMIT claim is no longer owned")

        current_report_input = self._build_report_input(connection, run)
        current_render_fingerprint = render_input_fingerprint(current_report_input.render_input)
        claimed_render_fingerprint = render_input_fingerprint(claim.report_input.render_input)
        if current_render_fingerprint != claimed_render_fingerprint:
            raise CommitFinalReportInputDriftError("authoritative RenderInput changed after COMMIT claim")

        current_input_fingerprint = final_report_input_fingerprint(current_report_input)
        if current_input_fingerprint != claim.input_fingerprint:
            raise CommitFinalReportInputDriftError("authoritative FinalReportInput changed after COMMIT claim")

        expected_bytes = render_final_report(current_report_input)
        if rendered.content != expected_bytes:
            raise CommitFinalReportIntegrityError(
                "focused/fresh report bytes do not match authoritative FinalReportInput",
            )
        expected_content_hash = f"sha256:{sha256(rendered.content).hexdigest()}"
        if rendered.content_hash != expected_content_hash:
            raise CommitFinalReportIntegrityError("rendered report hash does not match rendered bytes")
        current_baseline_output = self._baseline_output(connection, run)
        self._require_matching_update_proof(
            current_report_input,
            claim.baseline_output,
            current_baseline_output,
            rendered.update_proof,
        )
        if artifact.content_hash != expected_content_hash:
            raise CommitFinalReportIntegrityError(
                "published artifact is not the deterministic report for authoritative FinalReportInput",
            )
        self._verify_published_artifact(artifact, expected_content_hash, len(rendered.content))

        output = output_repository.create(
            run.run_id,
            _OUTPUT_TYPE,
            artifact.storage_uri,
            expected_content_hash,
        )
        AuditEventRepository(connection).create(
            run.run_id,
            _AUDIT_ENTITY_TYPE,
            output.output_id,
            _AUDIT_EVENT_TYPE,
            claim.actor_type,
            claim.actor_id,
            self._audit_payload(
                run.run_id,
                current_render_fingerprint,
                output,
                current_report_input,
                current_baseline_output,
                rendered.update_proof,
            ),
        )

        closed_step = RunStepRepository(connection).update_execution_state(
            step.step_id,
            RunStepStatus.COMPLETED,
            mark_completed=True,
        )
        if closed_step is None:
            raise CommitFinalReportIntegrityError("COMMIT RunStep disappeared during finalization")
        RunMetricRepository(connection).create_for_closed_step(run.run_id, RunStage.COMMIT, step.attempt)

        completed_claim = idempotency_repository.mark_completed(
            _SCOPE,
            key,
            claim.owner_generation,
            self._reference(run.run_id, current_input_fingerprint, expected_content_hash),
        )
        if completed_claim is None:
            raise CommitFinalReportClaimLostError("COMMIT claim was lost during finalization")

        completed_run = RunRepository(connection).update_status(run.run_id, RunStatus.COMPLETED)
        if completed_run is None:
            raise CommitFinalReportIntegrityError("Run disappeared during COMMIT finalization")
        return CommitFinalReportResult(run.run_id, output, current_input_fingerprint, replayed=False)

    def _verify_published_artifact(
        self,
        artifact: StoredArtifact,
        expected_content_hash: str,
        expected_size: int,
    ) -> None:
        try:
            verified = self._artifact_storage.verify(artifact.storage_uri, artifact.content_hash)
        except (ArtifactIntegrityError, OSError) as error:
            raise CommitFinalReportIntegrityError("published final report artifact cannot be verified") from error
        if (
            verified.content_hash != expected_content_hash
            or artifact.content_hash != expected_content_hash
            or verified.size_bytes != expected_size
        ):
            raise CommitFinalReportIntegrityError(
                "published final report artifact differs from deterministic report bytes",
            )

    @staticmethod
    def _audit_payload(
        run_id: str,
        render_input_fingerprint_value: str,
        output: OutputRecord,
        report_input: FinalReportInput,
        baseline_output: OutputRecord | None,
        update_proof: FocusedUpdateProof | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "run_id": run_id,
            "output_type": _OUTPUT_TYPE,
            "render_input_fingerprint": render_input_fingerprint_value,
            "artifact_content_hash": output.content_hash,
            "storage_uri": output.storage_uri,
        }
        if report_input.update_change_set is not None:
            if baseline_output is None or update_proof is None:
                raise CommitFinalReportIntegrityError("UPDATE audit is missing focused patch proof")
            focused_update = update_proof.to_payload()
            focused_update.update({
                "baseline_run_id": report_input.update_change_set.baseline_run_id,
                "baseline_output_id": baseline_output.output_id,
            })
            payload["focused_update"] = focused_update
        elif baseline_output is not None or update_proof is not None:
            raise CommitFinalReportIntegrityError("non-UPDATE report cannot carry focused patch proof")
        return payload

    def _mark_publication_failed(self, claim: _Claim) -> None:
        with self._connection_pool.connection() as connection:
            failed = IdempotencyRepository(connection).mark_failed(
                _SCOPE,
                self._key(claim.run.run_id),
                claim.owner_generation,
                IdempotencyFailureKind.RETRYABLE_ARTIFACT_PUBLICATION,
            )
            if failed is None:
                raise CommitFinalReportClaimLostError("COMMIT failure ownership was lost")
            connection.commit()

    @staticmethod
    def _key(run_id: str) -> str:
        return f"{_KEY_PREFIX}{run_id}"

    @staticmethod
    def _reference(run_id: str, input_fingerprint: str, artifact_hash: str) -> str:
        return f"final-report:{run_id}:{input_fingerprint}:markdown-v1:{artifact_hash}"

    @staticmethod
    def _normalize(command: CommitFinalReportCommand) -> CommitFinalReportCommand:
        try:
            run_id = str(UUID(command.run_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidCommitFinalReportCommandError("run_id must be a valid UUID") from error
        if not command.actor_type or not command.actor_id:
            raise InvalidCommitFinalReportCommandError("actor_type and actor_id are required")
        return CommitFinalReportCommand(run_id, command.actor_type, command.actor_id)
