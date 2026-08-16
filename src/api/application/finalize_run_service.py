import re
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.build_update_change_set_service import (
    build_current_update_change_set_manifest,
)
from domain.render_input import (
    RenderInputManifest,
    build_render_input_manifest,
    render_input_fingerprint,
)
from domain.review_state import ReviewStateMembership, build_review_state_snapshot
from domain.run import RunStatus, RunType
from domain.update_review import UpdateReviewDecisionType, update_change_set_fingerprint
from repositories.audit_event_repository import AuditEventRecord, AuditEventRepository
from repositories.output_repository import OutputRecord, OutputRepository
from repositories.render_input_repository import RenderInputRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from repositories.update_review_decision_repository import UpdateReviewDecisionRepository
from services.local_artifact_storage import ArtifactIntegrityError, LocalArtifactStorage

_MAX_TRANSACTION_ATTEMPTS = 3
_OUTPUT_TYPE = "FINAL_REPORT"
_AUDIT_ENTITY_TYPE = "output"
_AUDIT_EVENT_TYPE = "FINAL_OUTPUT_COMMITTED"
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class FinalizeRunApplicationError(Exception):
    """Base class for final output preparation and finalization failures."""


class InvalidFinalizeRunCommandError(FinalizeRunApplicationError):
    pass


class FinalizeRunNotFoundError(FinalizeRunApplicationError):
    pass


class FinalizeRunNotEligibleError(FinalizeRunApplicationError):
    pass


class ReviewIncompleteError(FinalizeRunApplicationError):
    pass


class MissingRunRuleScopeError(FinalizeRunApplicationError):
    pass


class RenderInputDriftError(FinalizeRunApplicationError):
    pass


class FinalOutputConflictError(FinalizeRunApplicationError):
    pass


class FinalizationIntegrityError(FinalizeRunApplicationError):
    pass


class FinalArtifactIntegrityError(FinalizeRunApplicationError):
    pass


class UpdateApprovalMissingError(FinalizeRunApplicationError):
    pass


class UpdateApprovalRejectedError(FinalizeRunApplicationError):
    pass


class UpdateApprovalStaleError(FinalizeRunApplicationError):
    pass


class DirectFreshFinalizationDisabledError(FinalizeRunApplicationError):
    """A fresh output must be committed through CommitFinalReportService."""


@dataclass(frozen=True)
class PrepareRunOutputCommand:
    run_id: str


@dataclass(frozen=True)
class PreparedRunOutput:
    manifest: RenderInputManifest
    render_input_fingerprint: str


@dataclass(frozen=True)
class FinalizeRunOutputCommand:
    run_id: str
    expected_render_input_fingerprint: str
    artifact_storage_uri: str
    artifact_content_hash: str
    actor_type: str
    actor_id: str


@dataclass(frozen=True)
class FinalizeRunOutputResult:
    output: OutputRecord
    audit_event: AuditEventRecord | None
    run: RunRecord
    render_input_fingerprint: str
    replayed: bool



class FinalizeRunService:
    def __init__(self, connection_pool: ConnectionPool, artifact_storage: LocalArtifactStorage) -> None:
        self._connection_pool = connection_pool
        self._artifact_storage = artifact_storage

    def prepare(self, command: PrepareRunOutputCommand) -> PreparedRunOutput:
        normalized_command = PrepareRunOutputCommand(run_id=self._normalize_uuid(command.run_id, "run_id"))
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    self._set_repeatable_read(connection)
                    result = self._prepare_once(connection, normalized_command)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def finalize(self, command: FinalizeRunOutputCommand) -> FinalizeRunOutputResult:
        normalized_command = self._normalize_finalize_command(command)
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    self._set_repeatable_read(connection)
                    result = self._finalize_once(connection, normalized_command)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _prepare_once(
        self,
        connection: Connection,
        command: PrepareRunOutputCommand,
    ) -> PreparedRunOutput:
        run = self._lock_run(connection, command.run_id)
        self._require_fresh_status(run)
        manifest = self._build_manifest(connection, command.run_id)
        return PreparedRunOutput(manifest, render_input_fingerprint(manifest))

    def _finalize_once(
        self,
        connection: Connection,
        command: FinalizeRunOutputCommand,
    ) -> FinalizeRunOutputResult:
        run = self._lock_run(connection, command.run_id)
        output_repository = OutputRepository(connection)
        audit_repository = AuditEventRepository(connection)
        existing_output = output_repository.get_by_run_and_type(command.run_id, _OUTPUT_TYPE)

        if existing_output is not None:
            return self._replay_or_conflict(
                run,
                existing_output,
                tuple(audit_repository.list_for_run(command.run_id)),
                command,
            )
        if run.status is RunStatus.COMPLETED:
            raise FinalizationIntegrityError("completed run has no canonical final output")
        self._require_fresh_status(run)

        # Preserve the legacy API's read-only validation surface so callers get
        # useful drift/approval/integrity errors, but never let this path write
        # an Output, audit event, or COMPLETED Run.  Fresh persistence is owned
        # exclusively by CommitFinalReportService.
        manifest = self._build_manifest(connection, command.run_id)
        if run.run_type is RunType.UPDATE:
            self._require_update_approval(connection, run, manifest)
        fingerprint = render_input_fingerprint(manifest)
        if fingerprint != command.expected_render_input_fingerprint:
            raise RenderInputDriftError("authoritative render input no longer matches preparation")
        self._verify_artifact(command.artifact_storage_uri, command.artifact_content_hash)
        raise DirectFreshFinalizationDisabledError(
            "fresh finalization is owned by CommitFinalReportService",
        )

    def _require_update_approval(
        self,
        connection: Connection,
        run: RunRecord,
        render_manifest: RenderInputManifest,
    ) -> None:
        change_set = UpdateChangeSetRepository(connection).get_by_run_id(run.run_id)
        if change_set is None:
            raise UpdateApprovalMissingError("UPDATE Run has no durable change set")
        change_set.to_manifest()
        decision = UpdateReviewDecisionRepository(connection).get_by_change_set_id(
            change_set.change_set_id,
        )
        if decision is None:
            raise UpdateApprovalMissingError("UPDATE change set has no approval")
        if decision.decision is UpdateReviewDecisionType.REJECTED:
            raise UpdateApprovalRejectedError("UPDATE change set was rejected")
        current_change_set = build_current_update_change_set_manifest(connection, run)
        if update_change_set_fingerprint(current_change_set) != change_set.change_set_fingerprint:
            raise UpdateApprovalStaleError("approved UPDATE change set no longer matches current state")

    def _replay_or_conflict(
        self,
        run: RunRecord,
        output: OutputRecord,
        audit_events: tuple[AuditEventRecord, ...],
        command: FinalizeRunOutputCommand,
    ) -> FinalizeRunOutputResult:
        bindings = tuple(
            event
            for event in audit_events
            if event.entity_type == _AUDIT_ENTITY_TYPE
            and event.entity_id == output.output_id
            and event.event_type == _AUDIT_EVENT_TYPE
        )
        if run.status is not RunStatus.COMPLETED or len(bindings) != 1:
            raise FinalizationIntegrityError("canonical output does not have one completed finalization binding")
        binding = bindings[0]
        expected_payload = self._audit_payload(command, output)
        if binding.payload != expected_payload:
            if not self._binding_matches_output(binding, output):
                raise FinalizationIntegrityError("final-output audit binding is malformed")
            raise FinalOutputConflictError("existing final output has a different durable identity")
        if output.storage_uri != command.artifact_storage_uri or output.content_hash != command.artifact_content_hash:
            raise FinalOutputConflictError("existing final output has a different artifact identity")
        self._verify_artifact(output.storage_uri, output.content_hash)
        return FinalizeRunOutputResult(
            output=output,
            audit_event=binding,
            run=run,
            render_input_fingerprint=command.expected_render_input_fingerprint,
            replayed=True,
        )

    def _build_manifest(self, connection: Connection, run_id: str) -> RenderInputManifest:
        review_state_records = RunFindingRepository(connection).list_review_state_for_run(run_id)
        review_state = build_review_state_snapshot(
            run_id,
            tuple(
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
                for record in review_state_records
            ),
        )
        if not review_state.is_complete:
            raise ReviewIncompleteError("run has pending finding memberships")
        source_record = RenderInputRepository(connection).load_for_run(run_id, review_state)
        if source_record.rule_scope is None:
            raise MissingRunRuleScopeError("run has no closed rule scope")
        return build_render_input_manifest(
            run_id,
            review_state,
            source_record.to_domain(),
            source_record.rule_scope,
        )

    def _lock_run(self, connection: Connection, run_id: str) -> RunRecord:
        run = RunRepository(connection).get_for_update(run_id)
        if run is None:
            raise FinalizeRunNotFoundError(f"run {run_id} does not exist")
        return run

    def _require_fresh_status(self, run: RunRecord) -> None:
        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise FinalizeRunNotEligibleError(
                f"run {run.run_id} is not awaiting finalization",
            )

    def _normalize_finalize_command(self, command: FinalizeRunOutputCommand) -> FinalizeRunOutputCommand:
        run_id = self._normalize_uuid(command.run_id, "run_id")
        if not _FINGERPRINT_PATTERN.fullmatch(command.expected_render_input_fingerprint):
            raise InvalidFinalizeRunCommandError("expected_render_input_fingerprint must be canonical SHA-256")
        return FinalizeRunOutputCommand(
            run_id=run_id,
            expected_render_input_fingerprint=command.expected_render_input_fingerprint,
            artifact_storage_uri=command.artifact_storage_uri,
            artifact_content_hash=command.artifact_content_hash,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
        )

    def _normalize_uuid(self, value: str, field_name: str) -> str:
        try:
            return str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidFinalizeRunCommandError(f"{field_name} must be a valid UUID") from error

    def _set_repeatable_read(self, connection: Connection) -> None:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

    def _verify_artifact(self, storage_uri: str, content_hash: str) -> None:
        try:
            self._artifact_storage.verify(storage_uri, content_hash)
        except (ArtifactIntegrityError, OSError) as error:
            raise FinalArtifactIntegrityError("final artifact cannot be verified") from error

    def _audit_payload(
        self,
        command: FinalizeRunOutputCommand,
        output: OutputRecord,
    ) -> dict[str, str]:
        return {
            "run_id": command.run_id,
            "output_type": _OUTPUT_TYPE,
            "render_input_fingerprint": command.expected_render_input_fingerprint,
            "artifact_content_hash": output.content_hash,
            "storage_uri": output.storage_uri,
        }

    def _binding_matches_output(self, binding: AuditEventRecord, output: OutputRecord) -> bool:
        payload = binding.payload
        if not isinstance(payload, dict):
            return False
        if set(payload) != {
            "run_id",
            "output_type",
            "render_input_fingerprint",
            "artifact_content_hash",
            "storage_uri",
        }:
            return False
        return (
            payload.get("run_id") == output.run_id
            and payload.get("output_type") == _OUTPUT_TYPE
            and payload.get("artifact_content_hash") == output.content_hash
            and payload.get("storage_uri") == output.storage_uri
            and isinstance(payload["render_input_fingerprint"], str)
            and _FINGERPRINT_PATTERN.fullmatch(payload["render_input_fingerprint"]) is not None
        )
