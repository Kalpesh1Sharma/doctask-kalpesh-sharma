"""Application use case for grounded Fact extraction during UNDERSTAND."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.idempotency_claim_takeover import (
    DEFAULT_STALE_CLAIM_AFTER,
    classify_in_progress,
    reclaim_if_stale,
    validate_stale_claim_after,
)
from api.application.prepare_update_run_service import (
    UpdatePreparationApplicationError,
    build_current_update_preparation_manifest,
)
from api.application.provider_usage_tracking import finish_provider_usage, start_provider_usage
from api.application.semantic_chunk_retrieval_service import (
    SemanticChunkRetrievalError,
    SemanticChunkRetrievalService,
)
from domain.evidence import EvidenceSpan
from domain.fact import Fact, FactStatus
from domain.fact_extraction import (
    FactEvidenceCandidate,
    FactExtractionCandidate,
    FactExtractionRequest,
    FactExtractionResponse,
    FactExtractionSourceChunk,
    UpdateFactExtractionRequest,
    fact_candidate_key,
    fact_extraction_input_fingerprint,
    normalize_uuid,
    sha256_text,
    update_fact_extraction_input_fingerprint,
)
from domain.run import RunStage, RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import update_preparation_fingerprint
from repositories.document_chunk_repository import DocumentChunkRecord, DocumentChunkRepository
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.idempotency_repository import (
    IdempotencyFailureKind,
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
    InProgressClaimState,
)
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_repository import RunRecord, RunRepository
from services.fact_extraction_provider import (
    FactExtractionInvalidResponseError,
    FactExtractionProvider,
    FactExtractionProviderError,
)

_IDEMPOTENCY_SCOPE = "fact-extraction-v1"
_UPDATE_IDEMPOTENCY_SCOPE = "fact-extraction-update-v2"
_PREPARATION_SCOPE = "update-preparation-v1"
_MAX_TRANSACTION_ATTEMPTS = 3
_DEFAULT_RETRIEVAL_QUERY = (
    "Material mortgage-document facts: parties, obligations, amounts, dates, terms, "
    "approvals, disclosures, settlement details, and compliance-relevant statements."
)


class FactExtractionApplicationError(Exception):
    """Base class for grounded Fact extraction failures."""


class InvalidFactExtractionCommandError(FactExtractionApplicationError):
    pass


class FactExtractionRunNotFoundError(FactExtractionApplicationError):
    pass


class FactExtractionRunNotEligibleError(FactExtractionApplicationError):
    pass


class FactExtractionInputConflictError(FactExtractionApplicationError):
    pass


class FactExtractionIdempotencyIntegrityError(FactExtractionApplicationError):
    pass


class FactExtractionInProgressError(FactExtractionApplicationError):
    pass


class FactExtractionPreviouslyFailedError(FactExtractionApplicationError):
    pass


class InvalidFactExtractionResponseError(FactExtractionApplicationError):
    pass


class SourceEvidenceValidationError(InvalidFactExtractionResponseError):
    pass


class SourceInputDriftError(FactExtractionApplicationError):
    pass


class FactExtractionClaimLostError(FactExtractionApplicationError):
    pass


class FactExtractionProviderFailure(FactExtractionApplicationError):
    pass


class UpdateFactExtractionPreparationError(FactExtractionApplicationError):
    pass


class UpdateFactExtractionTargetStateConflictError(FactExtractionApplicationError):
    pass


class LegacyUpdateFactExtractionConflictError(FactExtractionApplicationError):
    pass


@dataclass(frozen=True)
class ExtractRunFactsCommand:
    run_id: str


@dataclass(frozen=True)
class ExtractRunFactsResult:
    run_id: str
    input_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _ClaimedOperation:
    request: FactExtractionRequest | UpdateFactExtractionRequest
    input_fingerprint: str
    claim_token: int
    idempotency_scope: str
    idempotency_key: str
    result_reference: str


@dataclass(frozen=True)
class _EvidencePlan:
    identity: tuple[str, str, int | None, int, int, str, str]
    span: EvidenceSpan


@dataclass(frozen=True)
class _FactPlan:
    fact: Fact
    evidence_identities: tuple[tuple[str, str, int | None, int, int, str, str], ...]


@dataclass(frozen=True)
class _PersistencePlan:
    evidence: tuple[_EvidencePlan, ...]
    facts: tuple[_FactPlan, ...]


class ExtractRunFactsService:
    def __init__(
        self,
        connection_pool: ConnectionPool,
        provider: FactExtractionProvider,
        trusted_instructions: str,
        instruction_version: str,
        *,
        stale_claim_after: timedelta = DEFAULT_STALE_CLAIM_AFTER,
        retrieval_service: SemanticChunkRetrievalService | None = None,
        retrieval_query: str = _DEFAULT_RETRIEVAL_QUERY,
    ) -> None:
        self._connection_pool = connection_pool
        self._provider = provider
        self._trusted_instructions = trusted_instructions
        self._instruction_version = instruction_version
        self._stale_claim_after = validate_stale_claim_after(stale_claim_after)
        if not retrieval_query.strip():
            raise ValueError("retrieval_query must not be empty")
        self._retrieval_service = retrieval_service
        self._retrieval_query = retrieval_query

    def extract(self, command: ExtractRunFactsCommand) -> ExtractRunFactsResult:
        normalized_command = ExtractRunFactsCommand(
            run_id=self._normalize_run_id(command.run_id),
        )
        claimed = self._claim_or_replay(normalized_command)
        if isinstance(claimed, ExtractRunFactsResult):
            return claimed

        return self._execute_claimed(claimed)

    def _execute_claimed(self, claimed: _ClaimedOperation) -> ExtractRunFactsResult:
        receipt = start_provider_usage(
            self._connection_pool,
            run_id=claimed.request.run_id,
            stage=RunStage.UNDERSTAND,
            operation_scope=claimed.idempotency_scope,
            operation_key=claimed.idempotency_key,
            claim_generation=claimed.claim_token,
            provider=self._provider,
        )
        try:
            try:
                response = self._provider.extract(claimed.request)
                persistence_plan = self._build_persistence_plan(claimed.request, response)
            finally:
                finish_provider_usage(
                    self._connection_pool, receipt, getattr(self._provider, "last_usage", None),
                )
        except SourceEvidenceValidationError:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_EVIDENCE_VALIDATION)
            raise
        except InvalidFactExtractionResponseError:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE)
            raise
        except FactExtractionApplicationError:
            self._mark_failed(claimed)
            raise
        except FactExtractionInvalidResponseError as error:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE)
            raise InvalidFactExtractionResponseError(
                "fact extraction provider returned invalid structured output",
            ) from error
        except FactExtractionProviderError as error:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE)
            raise FactExtractionProviderFailure("fact extraction provider failed") from error
        except Exception as error:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE)
            raise FactExtractionProviderFailure("fact extraction provider failed") from error

        self._persist(claimed, persistence_plan)
        return ExtractRunFactsResult(
            run_id=claimed.request.run_id,
            input_fingerprint=claimed.input_fingerprint,
            replayed=False,
        )

    def _retry_after_failed_from_workflow(
        self,
        command: ExtractRunFactsCommand,
        *,
        expected_failed_generation: int,
    ) -> ExtractRunFactsResult:
        """Internal Phase 5C path; normal ``extract`` never reopens FAILED."""
        normalized = ExtractRunFactsCommand(run_id=self._normalize_run_id(command.run_id))
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(normalized.run_id)
            if run is None:
                raise FactExtractionRunNotFoundError(f"run {normalized.run_id} does not exist")
            scope, key = self._fact_extraction_identity_for_retry(run)
            repository = IdempotencyRepository(connection)
            failed = repository.get(scope, key)
            if (
                failed is None
                or failed.status is not IdempotencyStatus.FAILED
                or failed.claim_generation != expected_failed_generation
                or failed.failure_kind is None
            ):
                return self._resolve_authoritative_retry_existing(connection, run, repository, scope, key)
            if run.status is not RunStatus.RUNNING:
                raise FactExtractionRunNotEligibleError("run is not running fact extraction")
            if run.run_type is RunType.UPDATE:
                request = self._load_update_request(connection, run, for_share=False)
                fingerprint = update_fact_extraction_input_fingerprint(request)
                reference = self._update_result_reference(run.run_id, fingerprint)
                self._require_fresh_update_fact_state(connection, request)
            else:
                request = self._load_request(connection, run.run_id, for_share=False)
                fingerprint = fact_extraction_input_fingerprint(request)
                reference = self._result_reference(run.run_id, fingerprint)
            reopened = repository.reopen_failed(scope, key, expected_failed_generation, failed.failure_kind)
            if reopened is None:
                return self._resolve_authoritative_retry_existing(connection, run, repository, scope, key)
            connection.commit()
        return self._execute_claimed(_ClaimedOperation(request, fingerprint, reopened.claim_generation, scope, key, reference))

    @staticmethod
    def _fact_extraction_identity_for_retry(run: RunRecord) -> tuple[str, str]:
        if run.run_type is RunType.UPDATE:
            return _UPDATE_IDEMPOTENCY_SCOPE, f"understand-update:{run.run_id}"
        return _IDEMPOTENCY_SCOPE, f"understand:{run.run_id}"

    def _resolve_authoritative_retry_existing(self, connection: Connection, run: RunRecord, repository: IdempotencyRepository, scope: str, key: str) -> ExtractRunFactsResult:
        current = repository.get(scope, key)
        if current is None:
            raise FactExtractionIdempotencyIntegrityError("failed retry operation disappeared")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise FactExtractionInProgressError("fact extraction is already in progress")
        if current.status is IdempotencyStatus.FAILED:
            raise FactExtractionPreviouslyFailedError("fact extraction previously failed")
        if scope == _UPDATE_IDEMPOTENCY_SCOPE:
            return self._resolve_completed_update_existing(connection, run, current)
        return self._resolve_completed_v1_existing(connection, run.run_id, current)

    def _claim_or_replay(
        self,
        command: ExtractRunFactsCommand,
    ) -> _ClaimedOperation | ExtractRunFactsResult:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    outcome = self._claim_or_replay_once(connection, command)
                    connection.commit()
                    return outcome
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _claim_or_replay_once(
        self,
        connection: Connection,
        command: ExtractRunFactsCommand,
    ) -> _ClaimedOperation | ExtractRunFactsResult:
        run = RunRepository(connection).get_for_update(command.run_id)
        if run is None:
            raise FactExtractionRunNotFoundError(f"run {command.run_id} does not exist")

        if run.run_type is RunType.UPDATE:
            return self._claim_or_replay_update_once(connection, run)

        idempotency = IdempotencyRepository(connection)
        key = self._idempotency_key(command.run_id)
        existing = idempotency.get(_IDEMPOTENCY_SCOPE, key)
        if existing is None:
            if run.status is not RunStatus.RUNNING:
                raise FactExtractionRunNotEligibleError(
                    f"run {command.run_id} is not running fact extraction",
                )
            request = self._load_request(connection, command.run_id, for_share=False)
            fingerprint = fact_extraction_input_fingerprint(request)
            result_reference = self._result_reference(command.run_id, fingerprint)
            claimed = idempotency.claim(_IDEMPOTENCY_SCOPE, key)
            if claimed is not None:
                return _ClaimedOperation(request, fingerprint, claimed.claim_generation, _IDEMPOTENCY_SCOPE, key, result_reference)
            return self._resolve_authoritative_v1_existing(connection, command.run_id, idempotency)
        if existing.status is IdempotencyStatus.FAILED:
            raise FactExtractionPreviouslyFailedError("fact extraction previously failed")
        if existing.status is IdempotencyStatus.COMPLETED:
            return self._resolve_completed_v1_existing(connection, command.run_id, existing)
        state = classify_in_progress(idempotency, existing, self._stale_claim_after)
        if state is InProgressClaimState.MATCHING_FRESH:
            raise FactExtractionInProgressError("fact extraction is already in progress")
        if state is InProgressClaimState.CHANGED_OR_MISSING:
            return self._resolve_authoritative_v1_existing(connection, command.run_id, idempotency)
        if run.status is not RunStatus.RUNNING:
            raise FactExtractionRunNotEligibleError(f"run {command.run_id} is not running fact extraction")
        request = self._load_request(connection, command.run_id, for_share=False)
        fingerprint = fact_extraction_input_fingerprint(request)
        result_reference = self._result_reference(command.run_id, fingerprint)
        claimed = reclaim_if_stale(idempotency, existing, self._stale_claim_after)
        if claimed is not None:
            return _ClaimedOperation(request, fingerprint, claimed.claim_generation, _IDEMPOTENCY_SCOPE, key, result_reference)
        return self._resolve_authoritative_v1_existing(connection, command.run_id, idempotency)

    def _claim_or_replay_update_once(
        self,
        connection: Connection,
        run: RunRecord,
    ) -> _ClaimedOperation | ExtractRunFactsResult:
        idempotency = IdempotencyRepository(connection)
        key = self._update_idempotency_key(run.run_id)
        existing = idempotency.get(_UPDATE_IDEMPOTENCY_SCOPE, key)
        if existing is not None:
            if existing.status is IdempotencyStatus.FAILED:
                raise FactExtractionPreviouslyFailedError("fact extraction previously failed")
            if existing.status is IdempotencyStatus.COMPLETED:
                return self._resolve_completed_update_existing(connection, run, existing)
            state = classify_in_progress(idempotency, existing, self._stale_claim_after)
            if state is InProgressClaimState.MATCHING_FRESH:
                raise FactExtractionInProgressError("fact extraction is already in progress")
            if state is InProgressClaimState.CHANGED_OR_MISSING:
                return self._resolve_authoritative_update_existing(connection, run, idempotency)
            if run.status is not RunStatus.RUNNING:
                raise FactExtractionRunNotEligibleError(f"run {run.run_id} is not running fact extraction")
            request = self._load_update_request(connection, run, for_share=False)
            fingerprint = update_fact_extraction_input_fingerprint(request)
            result_reference = self._update_result_reference(run.run_id, fingerprint)
            self._require_fresh_update_fact_state(connection, request)
            claimed = reclaim_if_stale(idempotency, existing, self._stale_claim_after)
            if claimed is not None:
                return _ClaimedOperation(request, fingerprint, claimed.claim_generation, _UPDATE_IDEMPOTENCY_SCOPE, key, result_reference)
            return self._resolve_authoritative_update_existing(connection, run, idempotency)

        legacy = idempotency.get(_IDEMPOTENCY_SCOPE, self._idempotency_key(run.run_id))
        if legacy is not None:
            raise LegacyUpdateFactExtractionConflictError(
                "UPDATE Run has a legacy full-scope fact extraction operation",
            )
        request = self._load_update_request(connection, run, for_share=False)
        fingerprint = update_fact_extraction_input_fingerprint(request)
        result_reference = self._update_result_reference(run.run_id, fingerprint)
        if run.status is not RunStatus.RUNNING:
            raise FactExtractionRunNotEligibleError(
                f"run {run.run_id} is not running fact extraction",
            )
        self._require_fresh_update_fact_state(connection, request)
        claimed = idempotency.claim(_UPDATE_IDEMPOTENCY_SCOPE, key)
        if claimed is None:
            existing = idempotency.get(_UPDATE_IDEMPOTENCY_SCOPE, key)
            if existing is None:
                raise FactExtractionIdempotencyIntegrityError("claimed operation disappeared")
            return self._resolve_existing(existing, request, fingerprint, result_reference)
        return _ClaimedOperation(
            request,
            fingerprint,
            claimed.claim_generation,
            _UPDATE_IDEMPOTENCY_SCOPE,
            key,
            result_reference,
        )

    def _resolve_authoritative_v1_existing(
        self, connection: Connection, run_id: str, idempotency: IdempotencyRepository,
    ) -> ExtractRunFactsResult:
        current = idempotency.get(_IDEMPOTENCY_SCOPE, self._idempotency_key(run_id))
        if current is None:
            raise FactExtractionIdempotencyIntegrityError("claimed operation disappeared")
        if current.status is IdempotencyStatus.FAILED:
            raise FactExtractionPreviouslyFailedError("fact extraction previously failed")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise FactExtractionInProgressError("fact extraction is already in progress")
        return self._resolve_completed_v1_existing(connection, run_id, current)

    def _resolve_completed_v1_existing(
        self, connection: Connection, run_id: str, existing: IdempotencyRecord,
    ) -> ExtractRunFactsResult:
        request = self._load_request(connection, run_id, for_share=False)
        fingerprint = fact_extraction_input_fingerprint(request)
        return self._resolve_existing(existing, request, fingerprint, self._result_reference(run_id, fingerprint))

    def _resolve_authoritative_update_existing(
        self, connection: Connection, run: RunRecord, idempotency: IdempotencyRepository,
    ) -> ExtractRunFactsResult:
        current = idempotency.get(_UPDATE_IDEMPOTENCY_SCOPE, self._update_idempotency_key(run.run_id))
        if current is None:
            raise FactExtractionIdempotencyIntegrityError("claimed operation disappeared")
        if current.status is IdempotencyStatus.FAILED:
            raise FactExtractionPreviouslyFailedError("fact extraction previously failed")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise FactExtractionInProgressError("fact extraction is already in progress")
        return self._resolve_completed_update_existing(connection, run, current)

    def _resolve_completed_update_existing(
        self, connection: Connection, run: RunRecord, existing: IdempotencyRecord,
    ) -> ExtractRunFactsResult:
        request = self._load_update_request(connection, run, for_share=False)
        fingerprint = update_fact_extraction_input_fingerprint(request)
        return self._resolve_existing(
            existing, request, fingerprint, self._update_result_reference(run.run_id, fingerprint),
        )

    def _resolve_existing(
        self,
        existing: IdempotencyRecord,
        request: FactExtractionRequest | UpdateFactExtractionRequest,
        fingerprint: str,
        result_reference: str,
    ) -> ExtractRunFactsResult:
        if existing.status is IdempotencyStatus.COMPLETED:
            if isinstance(request, UpdateFactExtractionRequest) and not self._is_update_reference(
                existing.result_reference,
                request.run_id,
            ):
                raise FactExtractionIdempotencyIntegrityError(
                    "completed UPDATE fact extraction has malformed result reference",
                )
            if existing.result_reference != result_reference:
                raise FactExtractionInputConflictError(
                    "the run already completed fact extraction for different source input",
                )
            return ExtractRunFactsResult(request.run_id, fingerprint, replayed=True)
        if existing.status is IdempotencyStatus.IN_PROGRESS:
            raise FactExtractionInProgressError("fact extraction is already in progress")
        raise FactExtractionPreviouslyFailedError("fact extraction previously failed")

    def _persist(self, claimed: _ClaimedOperation, plan: _PersistencePlan) -> None:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    self._persist_once(connection, claimed, plan)
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _persist_once(
        self,
        connection: Connection,
        claimed: _ClaimedOperation,
        plan: _PersistencePlan,
    ) -> None:
        run = RunRepository(connection).get_for_update(claimed.request.run_id)
        if run is None:
            raise FactExtractionRunNotFoundError(
                f"run {claimed.request.run_id} does not exist",
            )
        if run.status is not RunStatus.RUNNING:
            raise FactExtractionRunNotEligibleError(
                f"run {claimed.request.run_id} is no longer running fact extraction",
            )

        if isinstance(claimed.request, UpdateFactExtractionRequest):
            try:
                current_request = self._load_update_request(connection, run, for_share=True)
            except UpdateFactExtractionPreparationError as error:
                raise SourceInputDriftError(
                    "UPDATE preparation state changed while provider work was in progress",
                ) from error
            current_fingerprint = update_fact_extraction_input_fingerprint(current_request)
            self._require_fresh_update_fact_state(connection, current_request)
        else:
            current_request = self._load_request(connection, claimed.request.run_id, for_share=True)
            current_fingerprint = fact_extraction_input_fingerprint(current_request)
        if current_fingerprint != claimed.input_fingerprint:
            raise SourceInputDriftError("run source input changed while provider work was in progress")

        evidence_repository = EvidenceSpanRepository(connection)
        fact_repository = FactRepository(connection)
        run_fact_repository = RunFactRepository(connection)
        fact_evidence_repository = FactEvidenceRepository(connection)
        evidence_ids: dict[tuple[str, str, int | None, int, int, str, str], str] = {}

        for evidence in plan.evidence:
            stored = evidence_repository.create(evidence.span)
            evidence_ids[evidence.identity] = stored.evidence_id

        for fact_plan in plan.facts:
            stored_fact = fact_repository.create(fact_plan.fact)
            run_fact_repository.add(
                claimed.request.run_id,
                stored_fact.fact_id,
                RunMembershipRelation.CREATED,
            )
            for identity in fact_plan.evidence_identities:
                fact_evidence_repository.add(stored_fact.fact_id, evidence_ids[identity])

        completed = IdempotencyRepository(connection).mark_completed(
            claimed.idempotency_scope,
            claimed.idempotency_key,
            claimed.claim_token,
            claimed.result_reference,
        )
        if completed is None:
            raise FactExtractionClaimLostError("fact extraction claim is no longer owned")

    def _mark_failed(
        self,
        claimed: _ClaimedOperation,
        failure_kind: IdempotencyFailureKind | None = None,
    ) -> None:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    failed = IdempotencyRepository(connection).mark_failed(
                        claimed.idempotency_scope,
                        claimed.idempotency_key,
                        claimed.claim_token,
                        failure_kind,
                    )
                    if failed is None:
                        raise FactExtractionClaimLostError("fact extraction claim is no longer owned")
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _load_request(
        self,
        connection: Connection,
        run_id: str,
        *,
        for_share: bool,
    ) -> FactExtractionRequest:
        version_ids = [
            membership.document_version_id
            for membership in RunDocumentVersionRepository(connection).list_for_run(run_id)
        ]
        chunk_repository = DocumentChunkRepository(connection)
        chunks = self._analysis_chunks(
            connection,
            chunk_repository,
            version_ids,
            for_share=for_share,
        )
        return FactExtractionRequest(
            run_id=run_id,
            instruction_version=self._instruction_version,
            trusted_instructions=self._trusted_instructions,
            document_version_ids=tuple(version_ids),
            source_chunks=tuple(
                FactExtractionSourceChunk(
                    chunk_id=chunk.chunk_id,
                    document_version_id=chunk.document_version_id,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    content=chunk.content,
                    content_digest=sha256_text(chunk.content),
                )
                for chunk in chunks
            ),
        )

    def _load_update_request(
        self,
        connection: Connection,
        run: RunRecord,
        *,
        for_share: bool,
    ) -> UpdateFactExtractionRequest:
        if (
            run.run_type is not RunType.UPDATE
            or run.baseline_run_id is None
            or run.trigger_document_version_id is None
        ):
            raise UpdateFactExtractionPreparationError(
                "UPDATE extraction requires an UPDATE Run with baseline and trigger",
            )
        try:
            preparation = build_current_update_preparation_manifest(connection, run)
        except UpdatePreparationApplicationError as error:
            raise UpdateFactExtractionPreparationError(
                "UPDATE preparation state is invalid",
            ) from error
        preparation_fingerprint = self._preparation_fingerprint(connection, run.run_id)
        expected_preparation_fingerprint = self._update_preparation_fingerprint(preparation)
        if preparation_fingerprint != expected_preparation_fingerprint:
            raise UpdateFactExtractionPreparationError(
                "completed UPDATE preparation does not match current preparation state",
            )

        expected_version_ids = {
            change.target_document_version_id for change in preparation.document_changes
        }
        target_memberships = RunDocumentVersionRepository(connection).list_for_run(run.run_id)
        target_version_ids = {
            membership.document_version_id for membership in target_memberships
        }
        if (
            len(target_memberships) != len(target_version_ids)
            or target_version_ids != expected_version_ids
            or run.trigger_document_version_id not in target_version_ids
        ):
            raise UpdateFactExtractionPreparationError(
                "UPDATE document scope does not match completed preparation",
            )

        run_facts = RunFactRepository(connection).list_for_run(run.run_id)
        carried_ids = {
            membership.fact_id
            for membership in run_facts
            if membership.relation is RunMembershipRelation.CARRIED_FORWARD
        }
        expected_carried_ids = set(preparation.carried_fact_ids)
        if carried_ids != expected_carried_ids:
            raise UpdateFactExtractionPreparationError(
                "UPDATE carried Fact memberships do not match completed preparation",
            )

        chunk_repository = DocumentChunkRepository(connection)
        chunks = self._analysis_chunks(
            connection,
            chunk_repository,
            (run.trigger_document_version_id,),
            for_share=for_share,
        )
        return UpdateFactExtractionRequest(
            run_id=run.run_id,
            baseline_run_id=run.baseline_run_id,
            trigger_document_version_id=run.trigger_document_version_id,
            preparation_fingerprint=preparation_fingerprint,
            instruction_version=self._instruction_version,
            trusted_instructions=self._trusted_instructions,
            document_version_ids=(run.trigger_document_version_id,),
            source_chunks=tuple(
                FactExtractionSourceChunk(
                    chunk_id=chunk.chunk_id,
                    document_version_id=chunk.document_version_id,
                    chunk_index=chunk.chunk_index,
                    page_number=chunk.page_number,
                    content=chunk.content,
                    content_digest=sha256_text(chunk.content),
                )
                for chunk in chunks
            ),
        )

    def _preparation_fingerprint(self, connection: Connection, run_id: str) -> str:
        record = IdempotencyRepository(connection).get(
            _PREPARATION_SCOPE,
            self._update_preparation_key(run_id),
        )
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            raise UpdateFactExtractionPreparationError(
                "UPDATE preparation has not completed",
            )
        if record.result_reference is None:
            raise UpdateFactExtractionPreparationError(
                "UPDATE preparation has malformed result reference",
            )
        prefix = f"update-preparation:{run_id}:"
        fingerprint = record.result_reference.removeprefix(prefix)
        if (
            not record.result_reference.startswith(prefix)
            or not self._is_fingerprint(fingerprint)
        ):
            raise UpdateFactExtractionPreparationError(
                "UPDATE preparation has malformed result reference",
            )
        return fingerprint

    def _require_fresh_update_fact_state(
        self,
        connection: Connection,
        request: UpdateFactExtractionRequest,
    ) -> None:
        if any(
            membership.relation is RunMembershipRelation.CREATED
            for membership in RunFactRepository(connection).list_for_run(request.run_id)
        ):
            raise UpdateFactExtractionTargetStateConflictError(
                "fresh UPDATE extraction cannot add to an existing created Fact set",
            )

    def _analysis_chunks(
        self,
        connection: Connection,
        repository: DocumentChunkRepository,
        version_ids: Iterable[str],
        *,
        for_share: bool,
    ) -> list[DocumentChunkRecord]:
        normalized_version_ids = tuple(version_ids)
        if self._retrieval_service is None:
            return self._list_chunks(
                repository,
                normalized_version_ids,
                for_share=for_share,
            )
        try:
            return list(
                self._retrieval_service.retrieve(
                    connection,
                    normalized_version_ids,
                    self._retrieval_query,
                    for_share=for_share,
                ),
            )
        except SemanticChunkRetrievalError as error:
            raise SourceInputDriftError(
                "analysis retrieval could not derive a grounded source scope",
            ) from error

    def _list_chunks(
        self,
        repository: DocumentChunkRepository,
        version_ids: Iterable[str],
        *,
        for_share: bool,
    ) -> list[DocumentChunkRecord]:
        chunks: list[DocumentChunkRecord] = []
        for version_id in version_ids:
            if for_share:
                chunks.extend(repository.list_for_document_version_for_share(version_id))
            else:
                chunks.extend(repository.list_for_document_version(version_id))
        return sorted(
            chunks,
            key=lambda chunk: (
                chunk.document_version_id,
                chunk.chunk_index,
                chunk.chunk_id,
            ),
        )

    def _build_persistence_plan(
        self,
        request: FactExtractionRequest,
        response: FactExtractionResponse,
    ) -> _PersistencePlan:
        if not isinstance(response, FactExtractionResponse):
            raise InvalidFactExtractionResponseError("provider returned an invalid response type")

        source_by_chunk_id = {chunk.chunk_id: chunk for chunk in request.source_chunks}
        grouped: dict[str, tuple[FactExtractionCandidate, set[tuple[str, str, int | None, int, int, str, str]]]] = {}
        evidence_spans: dict[tuple[str, str, int | None, int, int, str, str], EvidenceSpan] = {}

        for candidate in response.candidates:
            candidate_key = fact_candidate_key(candidate)
            if candidate_key not in grouped:
                grouped[candidate_key] = (candidate, set())
            evidence_identities = grouped[candidate_key][1]
            for evidence in candidate.evidence:
                identity, span = self._validate_evidence(evidence, source_by_chunk_id)
                evidence_identities.add(identity)
                evidence_spans.setdefault(identity, span)

        evidence_plan = tuple(
            _EvidencePlan(identity, evidence_spans[identity])
            for identity in sorted(evidence_spans)
        )
        fact_plan = tuple(
            _FactPlan(
                Fact(
                    fact_id=str(uuid4()),
                    origin_run_id=request.run_id,
                    subject=candidate.subject,
                    predicate=candidate.predicate,
                    object_value=candidate.object_value,
                    status=FactStatus.CONFIRMED,
                ),
                tuple(sorted(evidence_identities)),
            )
            for _, (candidate, evidence_identities) in sorted(grouped.items())
        )
        return _PersistencePlan(evidence_plan, fact_plan)

    def _validate_evidence(
        self,
        evidence: FactEvidenceCandidate,
        source_by_chunk_id: dict[str, FactExtractionSourceChunk],
    ) -> tuple[tuple[str, str, int | None, int, int, str, str], EvidenceSpan]:
        source = source_by_chunk_id.get(evidence.chunk_id)
        if source is None:
            raise SourceEvidenceValidationError("evidence references a chunk outside the request")

        start_offset = evidence.relative_start_offset
        end_offset = evidence.relative_end_offset
        offsets_match = (
            end_offset <= len(source.content)
            and source.content[start_offset:end_offset] == evidence.quoted_text
        )
        if not offsets_match:
            start_offset, end_offset = self._derive_unique_exact_quote_offsets(
                source.content,
                evidence.quoted_text,
            )

        content_hash = sha256_text(evidence.quoted_text)
        identity = (
            source.document_version_id,
            source.chunk_id,
            source.page_number,
            start_offset,
            end_offset,
            evidence.quoted_text,
            content_hash,
        )
        return identity, EvidenceSpan(
            evidence_id=str(uuid4()),
            document_version_id=source.document_version_id,
            chunk_id=source.chunk_id,
            page_number=source.page_number,
            start_offset=start_offset,
            end_offset=end_offset,
            source_text=evidence.quoted_text,
            content_hash=content_hash,
        )

    @staticmethod
    def _derive_unique_exact_quote_offsets(content: str, quoted_text: str) -> tuple[int, int]:
        first_start = content.find(quoted_text)
        if first_start < 0:
            raise SourceEvidenceValidationError("evidence quote does not match source chunk content")

        second_start = content.find(quoted_text, first_start + 1)
        if second_start >= 0:
            raise SourceEvidenceValidationError(
                "evidence quote is ambiguous within source chunk content",
            )

        return first_start, first_start + len(quoted_text)

    def _normalize_run_id(self, value: str) -> str:
        try:
            return normalize_uuid(value, "run_id")
        except ValueError as error:
            raise InvalidFactExtractionCommandError("run_id must be a valid UUID") from error

    def _idempotency_key(self, run_id: str) -> str:
        return f"understand:{run_id}"

    def _result_reference(self, run_id: str, fingerprint: str) -> str:
        return f"fact-extraction:{run_id}:{fingerprint}"

    @staticmethod
    def _update_idempotency_key(run_id: str) -> str:
        return f"understand-update:{run_id}"

    @staticmethod
    def _update_preparation_key(run_id: str) -> str:
        return f"prepare-update:{run_id}"

    @staticmethod
    def _update_result_reference(run_id: str, fingerprint: str) -> str:
        return f"fact-extraction-update:{run_id}:{fingerprint}"

    @staticmethod
    def _update_preparation_fingerprint(preparation) -> str:
        return update_preparation_fingerprint(preparation)

    @staticmethod
    def _is_fingerprint(value: str) -> bool:
        return (
            value.startswith("sha256:")
            and len(value) == len("sha256:") + 64
            and all(character in "0123456789abcdef" for character in value[7:])
        )

    @classmethod
    def _is_update_reference(cls, value: str | None, run_id: str) -> bool:
        if value is None:
            return False
        prefix = f"fact-extraction-update:{run_id}:"
        return value.startswith(prefix) and cls._is_fingerprint(value.removeprefix(prefix))
