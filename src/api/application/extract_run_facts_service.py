"""Application use case for grounded Fact extraction during UNDERSTAND."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from domain.evidence import EvidenceSpan
from domain.fact import Fact, FactStatus
from domain.fact_extraction import (
    FactEvidenceCandidate,
    FactExtractionCandidate,
    FactExtractionRequest,
    FactExtractionResponse,
    FactExtractionSourceChunk,
    fact_candidate_key,
    fact_extraction_input_fingerprint,
    normalize_uuid,
    sha256_text,
)
from domain.run import RunStatus
from domain.run_membership import RunMembershipRelation
from repositories.document_chunk_repository import DocumentChunkRecord, DocumentChunkRepository
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.idempotency_repository import (
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
)
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_repository import RunRepository
from services.fact_extraction_provider import FactExtractionProvider, FactExtractionProviderError

_IDEMPOTENCY_SCOPE = "fact-extraction-v1"
_MAX_TRANSACTION_ATTEMPTS = 3


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
    request: FactExtractionRequest
    input_fingerprint: str
    claim_token: datetime


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
    ) -> None:
        self._connection_pool = connection_pool
        self._provider = provider
        self._trusted_instructions = trusted_instructions
        self._instruction_version = instruction_version

    def extract(self, command: ExtractRunFactsCommand) -> ExtractRunFactsResult:
        normalized_command = ExtractRunFactsCommand(
            run_id=self._normalize_run_id(command.run_id),
        )
        claimed = self._claim_or_replay(normalized_command)
        if isinstance(claimed, ExtractRunFactsResult):
            return claimed

        try:
            response = self._provider.extract(claimed.request)
            persistence_plan = self._build_persistence_plan(claimed.request, response)
        except FactExtractionApplicationError:
            self._mark_failed(claimed)
            raise
        except FactExtractionProviderError as error:
            self._mark_failed(claimed)
            raise FactExtractionProviderFailure("fact extraction provider failed") from error
        except Exception as error:
            self._mark_failed(claimed)
            raise FactExtractionProviderFailure("fact extraction provider failed") from error

        self._persist(claimed, persistence_plan)
        return ExtractRunFactsResult(
            run_id=claimed.request.run_id,
            input_fingerprint=claimed.input_fingerprint,
            replayed=False,
        )

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

        request = self._load_request(connection, command.run_id, for_share=False)
        fingerprint = fact_extraction_input_fingerprint(request)
        result_reference = self._result_reference(command.run_id, fingerprint)
        idempotency = IdempotencyRepository(connection)
        existing = idempotency.get(_IDEMPOTENCY_SCOPE, self._idempotency_key(command.run_id))
        if existing is not None:
            return self._resolve_existing(existing, request, fingerprint, result_reference)

        if run.status is not RunStatus.RUNNING:
            raise FactExtractionRunNotEligibleError(
                f"run {command.run_id} is not running fact extraction",
            )

        claimed = idempotency.claim(_IDEMPOTENCY_SCOPE, self._idempotency_key(command.run_id))
        if claimed is None:
            existing = idempotency.get(_IDEMPOTENCY_SCOPE, self._idempotency_key(command.run_id))
            if existing is None:
                raise FactExtractionIdempotencyIntegrityError("claimed operation disappeared")
            return self._resolve_existing(existing, request, fingerprint, result_reference)
        if claimed.claimed_at is None:
            raise FactExtractionIdempotencyIntegrityError("claim has no fencing token")

        return _ClaimedOperation(request, fingerprint, claimed.claimed_at)

    def _resolve_existing(
        self,
        existing: IdempotencyRecord,
        request: FactExtractionRequest,
        fingerprint: str,
        result_reference: str,
    ) -> ExtractRunFactsResult:
        if existing.status is IdempotencyStatus.COMPLETED:
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

        current_request = self._load_request(connection, claimed.request.run_id, for_share=True)
        if fact_extraction_input_fingerprint(current_request) != claimed.input_fingerprint:
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
            _IDEMPOTENCY_SCOPE,
            self._idempotency_key(claimed.request.run_id),
            claimed.claim_token,
            self._result_reference(claimed.request.run_id, claimed.input_fingerprint),
        )
        if completed is None:
            raise FactExtractionClaimLostError("fact extraction claim is no longer owned")

    def _mark_failed(self, claimed: _ClaimedOperation) -> None:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    IdempotencyRepository(connection).mark_failed(
                        _IDEMPOTENCY_SCOPE,
                        self._idempotency_key(claimed.request.run_id),
                        claimed.claim_token,
                    )
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
        chunks = self._list_chunks(chunk_repository, version_ids, for_share=for_share)
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
        if evidence.relative_end_offset > len(source.content):
            raise SourceEvidenceValidationError("evidence offsets exceed source chunk content")
        if (
            source.content[evidence.relative_start_offset : evidence.relative_end_offset]
            != evidence.quoted_text
        ):
            raise SourceEvidenceValidationError("evidence quote does not match source chunk content")

        content_hash = sha256_text(evidence.quoted_text)
        identity = (
            source.document_version_id,
            source.chunk_id,
            source.page_number,
            evidence.relative_start_offset,
            evidence.relative_end_offset,
            evidence.quoted_text,
            content_hash,
        )
        return identity, EvidenceSpan(
            evidence_id=str(uuid4()),
            document_version_id=source.document_version_id,
            chunk_id=source.chunk_id,
            page_number=source.page_number,
            start_offset=evidence.relative_start_offset,
            end_offset=evidence.relative_end_offset,
            source_text=evidence.quoted_text,
            content_hash=content_hash,
        )

    def _normalize_run_id(self, value: str) -> str:
        try:
            return normalize_uuid(value, "run_id")
        except ValueError as error:
            raise InvalidFactExtractionCommandError("run_id must be a valid UUID") from error

    def _idempotency_key(self, run_id: str) -> str:
        return f"understand:{run_id}"

    def _result_reference(self, run_id: str, fingerprint: str) -> str:
        return f"fact-extraction:{run_id}:{fingerprint}"
