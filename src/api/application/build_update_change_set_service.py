import re
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.effective_run_fact_state import (
    EffectiveRunFactStateIntegrityError,
    list_effective_committed_facts,
)
from domain.render_input import (
    RenderInputManifest,
    build_render_input_manifest,
    render_input_fingerprint,
)
from domain.review_state import (
    ReviewStateMembership,
    build_review_state_snapshot,
    review_state_fingerprint,
)
from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import (
    UpdateDocumentChange,
    UpdateDocumentChangeKind,
    UpdatePreparationManifest,
    update_preparation_fingerprint,
)
from domain.update_review import (
    UpdateChangeSetInputIntegrityError,
    UpdateChangeSetManifest,
    classify_update_fact_delta,
    update_change_set_fingerprint,
)
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from repositories.render_input_repository import RenderInputRepository
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.update_change_set_repository import (
    UpdateChangeSetRecord,
    UpdateChangeSetRepository,
)

_MAX_TRANSACTION_ATTEMPTS = 3
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class UpdateChangeSetApplicationError(Exception):
    """Base class for UPDATE change-set construction failures."""


class UpdateChangeSetRunNotFoundError(UpdateChangeSetApplicationError):
    pass


class UpdateChangeSetRunNotEligibleError(UpdateChangeSetApplicationError):
    pass


class UpdateChangeSetPreparationError(UpdateChangeSetApplicationError):
    pass


class UpdateChangeSetReviewIncompleteError(UpdateChangeSetApplicationError):
    pass


class UpdateChangeSetRuleScopeError(UpdateChangeSetApplicationError):
    pass


class UpdateChangeSetConflictError(UpdateChangeSetApplicationError):
    pass


@dataclass(frozen=True)
class BuildUpdateChangeSetCommand:
    run_id: str


@dataclass(frozen=True)
class BuildUpdateChangeSetResult:
    change_set: UpdateChangeSetRecord
    manifest: UpdateChangeSetManifest
    replayed: bool


class BuildUpdateChangeSetService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def build(self, command: BuildUpdateChangeSetCommand) -> BuildUpdateChangeSetResult:
        run_id = normalize_run_id(command.run_id)
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    result = self._build_once(connection, run_id)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _build_once(self, connection: Connection, run_id: str) -> BuildUpdateChangeSetResult:
        run = RunRepository(connection).get_for_update(run_id)
        if run is None:
            raise UpdateChangeSetRunNotFoundError(f"run {run_id} does not exist")
        repository = UpdateChangeSetRepository(connection)
        existing = repository.get_by_run_id(run_id)

        if existing is not None:
            stored_manifest = existing.to_manifest()
            current_manifest = build_current_update_change_set_manifest(connection, run)
            if update_change_set_fingerprint(current_manifest) != existing.change_set_fingerprint:
                raise UpdateChangeSetConflictError("existing change set differs from current UPDATE state")
            return BuildUpdateChangeSetResult(existing, stored_manifest, replayed=True)

        require_fresh_build_run(run)
        current_manifest = build_current_update_change_set_manifest(connection, run)
        record = repository.create(current_manifest)
        return BuildUpdateChangeSetResult(record, current_manifest, replayed=False)


def normalize_run_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise UpdateChangeSetRunNotEligibleError("run_id must be a valid UUID") from error


def require_fresh_build_run(run: RunRecord) -> None:
    if run.run_type is not RunType.UPDATE or run.status is not RunStatus.REVIEW_REQUIRED:
        raise UpdateChangeSetRunNotEligibleError(
            f"run {run.run_id} is not an UPDATE awaiting review",
        )


def build_current_update_change_set_manifest(
    connection: Connection,
    run: RunRecord,
) -> UpdateChangeSetManifest:
    if run.baseline_run_id is None or run.trigger_document_version_id is None:
        raise UpdateChangeSetInputIntegrityError("UPDATE Run is missing baseline or trigger identity")
    render_manifest = build_current_render_input_manifest(connection, run.run_id)

    document_repository = RunDocumentVersionRepository(connection)
    baseline_scope = document_repository.list_scope_for_run(run.baseline_run_id)
    update_scope = document_repository.list_scope_for_run(run.run_id)
    document_delta = build_document_delta(
        baseline_scope,
        update_scope,
        run.trigger_document_version_id,
    )

    fact_repository = RunFactRepository(connection)
    try:
        baseline_facts = list_effective_committed_facts(connection, run.baseline_run_id)
    except EffectiveRunFactStateIntegrityError as error:
        raise UpdateChangeSetInputIntegrityError(
            "baseline committed Fact state cannot be reconstructed",
        ) from error
    update_facts = fact_repository.list_for_run(run.run_id)
    fact_delta = classify_update_fact_delta(
        (record.fact_id for record in baseline_facts),
        ((record.fact_id, record.relation) for record in update_facts),
    )
    expected_preparation_fingerprint = update_preparation_fingerprint(
        UpdatePreparationManifest(
            run_id=run.run_id,
            baseline_run_id=run.baseline_run_id,
            trigger_document_version_id=run.trigger_document_version_id,
            document_changes=document_delta,
            carried_fact_ids=tuple(
                record.fact_id
                for record in update_facts
                if record.relation is RunMembershipRelation.CARRIED_FORWARD
            ),
        ),
    )
    preparation_fingerprint = load_preparation_fingerprint(
        connection,
        run.run_id,
        expected_preparation_fingerprint,
    )

    return UpdateChangeSetManifest(
        run_id=run.run_id,
        baseline_run_id=run.baseline_run_id,
        trigger_document_version_id=run.trigger_document_version_id,
        preparation_fingerprint=preparation_fingerprint,
        document_delta=document_delta,
        fact_delta=fact_delta,
        review_state_fingerprint=review_state_fingerprint(render_manifest.review_state),
        render_input_fingerprint=render_input_fingerprint(render_manifest),
    )


def load_preparation_fingerprint(
    connection: Connection,
    run_id: str,
    expected_fingerprint: str,
) -> str:
    record = IdempotencyRepository(connection).get(
        "update-preparation-v1",
        f"prepare-update:{run_id}",
    )
    if record is None or record.status is not IdempotencyStatus.COMPLETED:
        raise UpdateChangeSetPreparationError("UPDATE preparation is not durably completed")
    result_reference = record.result_reference
    prefix = f"update-preparation:{run_id}:"
    if result_reference is None or not result_reference.startswith(prefix):
        raise UpdateChangeSetPreparationError("UPDATE preparation result reference is malformed")
    fingerprint = result_reference.removeprefix(prefix)
    if _FINGERPRINT_PATTERN.fullmatch(fingerprint) is None:
        raise UpdateChangeSetPreparationError("UPDATE preparation result reference is malformed")
    if fingerprint != expected_fingerprint:
        raise UpdateChangeSetPreparationError(
            "UPDATE preparation result reference does not match current durable preparation state",
        )
    return fingerprint


def build_current_render_input_manifest(connection: Connection, run_id: str) -> RenderInputManifest:
    review_records = RunFindingRepository(connection).list_review_state_for_run(run_id)
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
            for record in review_records
        ),
    )
    if not review_state.is_complete:
        raise UpdateChangeSetReviewIncompleteError("run has pending Finding memberships")
    source = RenderInputRepository(connection).load_for_run(run_id, review_state)
    if source.rule_scope is None:
        raise UpdateChangeSetRuleScopeError("run has no closed rule scope")
    return build_render_input_manifest(run_id, review_state, source.to_domain(), source.rule_scope)


def build_document_delta(
    baseline_scope: list,
    update_scope: list,
    trigger_document_version_id: str,
) -> tuple[UpdateDocumentChange, ...]:
    baseline_by_document = {record.document_id: record for record in baseline_scope}
    update_by_document = {record.document_id: record for record in update_scope}
    if len(baseline_by_document) != len(baseline_scope) or len(update_by_document) != len(update_scope):
        raise UpdateChangeSetInputIntegrityError("document scope contains multiple versions for one document")
    if trigger_document_version_id not in {record.document_version_id for record in update_scope}:
        raise UpdateChangeSetInputIntegrityError("UPDATE document scope does not contain its trigger")
    if set(baseline_by_document) - set(update_by_document):
        raise UpdateChangeSetInputIntegrityError("removed-document semantics are not supported")

    changes: list[UpdateDocumentChange] = []
    for document_id, update_record in update_by_document.items():
        baseline_record = baseline_by_document.get(document_id)
        if baseline_record is None:
            if update_record.document_version_id != trigger_document_version_id:
                raise UpdateChangeSetInputIntegrityError("only the trigger may add a document to an UPDATE")
            kind = UpdateDocumentChangeKind.ADDED
            baseline_version_id = None
        elif update_record.document_version_id == baseline_record.document_version_id:
            kind = UpdateDocumentChangeKind.RETAINED
            baseline_version_id = baseline_record.document_version_id
        else:
            if update_record.document_version_id != trigger_document_version_id:
                raise UpdateChangeSetInputIntegrityError("only the trigger may replace a baseline document")
            if update_record.version_number <= baseline_record.version_number:
                raise UpdateChangeSetInputIntegrityError("replacement trigger must advance the document version")
            kind = UpdateDocumentChangeKind.REPLACED
            baseline_version_id = baseline_record.document_version_id
        changes.append(
            UpdateDocumentChange(
                document_id=document_id,
                kind=kind,
                baseline_document_version_id=baseline_version_id,
                target_document_version_id=update_record.document_version_id,
                target_version_number=update_record.version_number,
                target_content_hash=update_record.content_hash,
            ),
        )
    return tuple(changes)
