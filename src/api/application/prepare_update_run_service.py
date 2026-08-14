"""Deterministic materialization of an UPDATE Run's current source state."""

import re
from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from domain.document_version import DocumentProcessingStatus
from domain.fact import FactStatus
from domain.fact_extraction import normalize_uuid
from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import (
    UpdateDocumentChange,
    UpdateDocumentChangeKind,
    UpdatePreparationManifest,
    update_preparation_fingerprint,
)
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.idempotency_repository import (
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
)
from repositories.run_document_version_repository import (
    RunDocumentVersionRepository,
    RunDocumentVersionScopeRecord,
)
from repositories.run_fact_repository import RunFactRepository
from repositories.run_repository import RunRecord, RunRepository

_SCOPE = "update-preparation-v1"
_MAX_ATTEMPTS = 3
_REFERENCE = re.compile(
    r"^update-preparation:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}:sha256:[0-9a-f]{64}$",
)


class UpdatePreparationApplicationError(Exception):
    pass


class InvalidUpdatePreparationCommandError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationRunNotFoundError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationRunNotEligibleError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationBaselineError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationTriggerError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationBaselineScopeError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationTargetStateConflictError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationInputConflictError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationIdempotencyIntegrityError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationInProgressError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationPreviouslyFailedError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationInputDriftError(UpdatePreparationApplicationError):
    pass


class UpdatePreparationClaimLostError(UpdatePreparationApplicationError):
    pass


@dataclass(frozen=True)
class PrepareUpdateRunCommand:
    run_id: str


@dataclass(frozen=True)
class PrepareUpdateRunResult:
    manifest: UpdatePreparationManifest
    input_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _Claim:
    manifest: UpdatePreparationManifest
    input_fingerprint: str
    token: datetime


class PrepareUpdateRunService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def prepare(self, command: PrepareUpdateRunCommand) -> PrepareUpdateRunResult:
        run_id = self._normalize_run_id(command.run_id)
        claimed = self._claim_or_replay(run_id)
        if isinstance(claimed, PrepareUpdateRunResult):
            return claimed
        self._persist(claimed)
        return PrepareUpdateRunResult(
            manifest=claimed.manifest,
            input_fingerprint=claimed.input_fingerprint,
            replayed=False,
        )

    def _claim_or_replay(self, run_id: str) -> _Claim | PrepareUpdateRunResult:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    outcome = self._claim_or_replay_once(connection, run_id)
                    connection.commit()
                    return outcome
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _claim_or_replay_once(
        self,
        connection: Connection,
        run_id: str,
    ) -> _Claim | PrepareUpdateRunResult:
        run = RunRepository(connection).get_for_update(run_id)
        if run is None:
            raise UpdatePreparationRunNotFoundError(f"run {run_id} does not exist")
        manifest = self._build_manifest(connection, run)
        fingerprint = update_preparation_fingerprint(manifest)
        reference = self._reference(run_id, fingerprint)
        idempotency = IdempotencyRepository(connection)
        existing = idempotency.get(_SCOPE, self._key(run_id))
        if existing is not None:
            return self._resolve_existing(existing, manifest, fingerprint, reference)

        self._validate_fresh_run(run)
        self._require_empty_target_state(connection, run_id)
        claimed = idempotency.claim(_SCOPE, self._key(run_id))
        if claimed is None:
            existing = idempotency.get(_SCOPE, self._key(run_id))
            if existing is None:
                raise UpdatePreparationIdempotencyIntegrityError("claimed operation disappeared")
            return self._resolve_existing(existing, manifest, fingerprint, reference)
        if claimed.claimed_at is None:
            raise UpdatePreparationIdempotencyIntegrityError("claim has no fencing token")
        return _Claim(manifest, fingerprint, claimed.claimed_at)

    def _persist(self, claim: _Claim) -> None:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    self._persist_once(connection, claim)
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _persist_once(self, connection: Connection, claim: _Claim) -> None:
        run = RunRepository(connection).get_for_update(claim.manifest.run_id)
        if run is None:
            raise UpdatePreparationRunNotFoundError(
                f"run {claim.manifest.run_id} does not exist",
            )
        self._validate_fresh_run(run)
        self._require_empty_target_state(connection, run.run_id)
        current = self._build_manifest(connection, run)
        if update_preparation_fingerprint(current) != claim.input_fingerprint:
            raise UpdatePreparationInputDriftError("UPDATE preparation input changed")

        documents = RunDocumentVersionRepository(connection)
        for change in claim.manifest.document_changes:
            documents.add(run.run_id, change.target_document_version_id)
        facts = RunFactRepository(connection)
        for fact_id in claim.manifest.carried_fact_ids:
            facts.add(run.run_id, fact_id, RunMembershipRelation.CARRIED_FORWARD)

        completed = IdempotencyRepository(connection).mark_completed(
            _SCOPE,
            self._key(run.run_id),
            claim.token,
            self._reference(run.run_id, claim.input_fingerprint),
        )
        if completed is None:
            raise UpdatePreparationClaimLostError("UPDATE preparation claim is no longer owned")

    def _build_manifest(
        self,
        connection: Connection,
        update_run: RunRecord,
    ) -> UpdatePreparationManifest:
        if update_run.baseline_run_id is None or update_run.trigger_document_version_id is None:
            raise UpdatePreparationRunNotEligibleError("UPDATE Run requires baseline and trigger")
        baseline = RunRepository(connection).get_by_id(update_run.baseline_run_id)
        if (
            baseline is None
            or baseline.pile_id != update_run.pile_id
            or baseline.status is not RunStatus.COMPLETED
        ):
            raise UpdatePreparationBaselineError("baseline must be a completed Run in the same pile")

        trigger = DocumentVersionRepository(connection).get_by_id(
            update_run.trigger_document_version_id,
        )
        if trigger is None:
            raise UpdatePreparationTriggerError("trigger document version does not exist")
        trigger_document = DocumentRepository(connection).get_by_id(trigger.document_id)
        if (
            trigger_document is None
            or trigger_document.pile_id != update_run.pile_id
            or trigger.processing_status is not DocumentProcessingStatus.READY
        ):
            raise UpdatePreparationTriggerError("trigger must be a READY version in the Run pile")

        baseline_scope = RunDocumentVersionRepository(connection).list_scope_for_run(
            baseline.run_id,
        )
        by_document: dict[str, RunDocumentVersionScopeRecord] = {}
        for item in baseline_scope:
            if (
                item.document_pile_id != update_run.pile_id
                or item.processing_status is not DocumentProcessingStatus.READY
            ):
                raise UpdatePreparationBaselineScopeError(
                    "baseline contains a non-READY or cross-pile document version",
                )
            if item.document_id in by_document:
                raise UpdatePreparationBaselineScopeError(
                    "baseline contains multiple versions for one document",
                )
            by_document[item.document_id] = item

        baseline_for_trigger = by_document.get(trigger.document_id)
        if baseline_for_trigger is not None:
            if baseline_for_trigger.document_version_id == trigger.document_version_id:
                raise UpdatePreparationTriggerError("trigger already belongs to baseline scope")
            if trigger.version_number <= baseline_for_trigger.version_number:
                raise UpdatePreparationTriggerError("trigger version must advance the baseline document")

        changes: list[UpdateDocumentChange] = []
        for document_id, item in by_document.items():
            if document_id == trigger.document_id:
                changes.append(UpdateDocumentChange(
                    document_id=document_id,
                    kind=UpdateDocumentChangeKind.REPLACED,
                    baseline_document_version_id=item.document_version_id,
                    target_document_version_id=trigger.document_version_id,
                    target_version_number=trigger.version_number,
                    target_content_hash=trigger.content_hash,
                ))
            else:
                changes.append(UpdateDocumentChange(
                    document_id=document_id,
                    kind=UpdateDocumentChangeKind.RETAINED,
                    baseline_document_version_id=item.document_version_id,
                    target_document_version_id=item.document_version_id,
                    target_version_number=item.version_number,
                    target_content_hash=item.content_hash,
                ))
        if baseline_for_trigger is None:
            changes.append(UpdateDocumentChange(
                document_id=trigger.document_id,
                kind=UpdateDocumentChangeKind.ADDED,
                baseline_document_version_id=None,
                target_document_version_id=trigger.document_version_id,
                target_version_number=trigger.version_number,
                target_content_hash=trigger.content_hash,
            ))

        retained_versions = {
            change.target_document_version_id
            for change in changes
            if change.kind is UpdateDocumentChangeKind.RETAINED
        }
        carried_fact_ids = tuple(
            record.fact_id
            for record in RunFactRepository(connection).list_reconciliation_input_for_run(
                baseline.run_id,
            )
            if record.status in {FactStatus.CONFIRMED, FactStatus.DISPUTED}
            and record.evidence
            and all(item.document_version_id in retained_versions for item in record.evidence)
        )
        return UpdatePreparationManifest(
            run_id=update_run.run_id,
            baseline_run_id=baseline.run_id,
            trigger_document_version_id=trigger.document_version_id,
            document_changes=tuple(changes),
            carried_fact_ids=carried_fact_ids,
        )

    def _resolve_existing(
        self,
        existing: IdempotencyRecord,
        manifest: UpdatePreparationManifest,
        fingerprint: str,
        reference: str,
    ) -> PrepareUpdateRunResult:
        if existing.status is IdempotencyStatus.COMPLETED:
            if existing.result_reference is None or not _REFERENCE.fullmatch(existing.result_reference):
                raise UpdatePreparationIdempotencyIntegrityError(
                    "completed UPDATE preparation has malformed result reference",
                )
            if existing.result_reference != reference:
                raise UpdatePreparationInputConflictError(
                    "UPDATE preparation completed for different input",
                )
            return PrepareUpdateRunResult(manifest, fingerprint, replayed=True)
        if existing.status is IdempotencyStatus.IN_PROGRESS:
            raise UpdatePreparationInProgressError("UPDATE preparation is already in progress")
        raise UpdatePreparationPreviouslyFailedError("UPDATE preparation previously failed")

    def _validate_fresh_run(self, run: RunRecord) -> None:
        if (
            run.run_type is not RunType.UPDATE
            or run.status is not RunStatus.PENDING
            or run.baseline_run_id is None
            or run.trigger_document_version_id is None
            or run.blocked_on_run_id is not None
        ):
            raise UpdatePreparationRunNotEligibleError(
                "fresh UPDATE preparation requires an unblocked PENDING UPDATE Run",
            )

    def _require_empty_target_state(self, connection: Connection, run_id: str) -> None:
        documents = RunDocumentVersionRepository(connection).list_for_run(run_id)
        facts = RunFactRepository(connection).list_for_run(run_id)
        if documents or facts:
            raise UpdatePreparationTargetStateConflictError(
                "fresh UPDATE Run already has materialized input or Fact memberships",
            )

    def _normalize_run_id(self, value: str) -> str:
        try:
            return normalize_uuid(value, "run_id")
        except ValueError as error:
            raise InvalidUpdatePreparationCommandError("run_id must be a valid UUID") from error

    @staticmethod
    def _key(run_id: str) -> str:
        return f"prepare-update:{run_id}"

    @staticmethod
    def _reference(run_id: str, fingerprint: str) -> str:
        return f"update-preparation:{run_id}:{fingerprint}"
