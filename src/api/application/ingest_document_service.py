"""Durable source ingestion: publish, claim, parse/embed, then make READY."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.idempotency_claim_takeover import (
    DEFAULT_STALE_CLAIM_AFTER,
    classify_in_progress,
    reclaim_if_stale,
    validate_stale_claim_after,
)
from domain.document_version import DocumentProcessingStatus, DocumentVersion
from repositories.document_chunk_repository import DocumentChunkRepository
from repositories.document_repository import DocumentRecord, DocumentRepository
from repositories.document_version_repository import (
    DocumentVersionRecord,
    DocumentVersionRepository,
)
from repositories.idempotency_repository import (
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
    InProgressClaimState,
)
from services.document_chunker import ParsedChunk, chunk_document
from services.document_parser import DocumentParser
from services.embedding_provider import (
    EmbeddingProvider,
    EmbeddingProviderError,
    validate_embeddings,
)
from services.local_source_storage import LocalSourceStorage, SourceInput, StoredSource

if TYPE_CHECKING:
    from api.application.document_arrival_service import DocumentArrivalService

_SCOPE = "document-ingestion-v1"
_REFERENCE = re.compile(
    r"^document-ingestion:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}:sha256:[0-9a-f]{64}$",
)
_MAX_ATTEMPTS = 3
_EMBEDDING_BATCH_SIZE = 64


class DocumentIngestionError(Exception):
    pass


class InvalidDocumentIngestionCommandError(DocumentIngestionError):
    pass


class DocumentIngestionPileNotFoundError(DocumentIngestionError):
    pass


class DocumentIngestionInProgressError(DocumentIngestionError):
    pass


class DocumentIngestionPreviouslyFailedError(DocumentIngestionError):
    pass


class DocumentIngestionIntegrityError(DocumentIngestionError):
    pass


class DocumentIngestionInputConflictError(DocumentIngestionError):
    pass


class DocumentIngestionClaimLostError(DocumentIngestionError):
    pass


class DocumentIngestionProcessingError(DocumentIngestionError):
    pass


@dataclass(frozen=True)
class IngestDocumentCommand:
    pile_id: str
    logical_key: str
    source: SourceInput


@dataclass(frozen=True)
class IngestDocumentResult:
    document_id: str
    document_version_id: str
    content_hash: str
    storage_uri: str
    replayed: bool


@dataclass(frozen=True)
class _Claim:
    pile_id: str
    logical_key: str
    document: DocumentRecord
    version: DocumentVersionRecord
    source: StoredSource
    key: str
    generation: int


class IngestDocumentService:
    def __init__(
        self,
        connection_pool: ConnectionPool,
        source_storage: LocalSourceStorage,
        parser: DocumentParser,
        embedding_provider: EmbeddingProvider,
        *,
        arrival_service: DocumentArrivalService | None = None,
        stale_claim_after: timedelta = DEFAULT_STALE_CLAIM_AFTER,
        embedding_batch_size: int = _EMBEDDING_BATCH_SIZE,
    ) -> None:
        if not isinstance(embedding_batch_size, int) or embedding_batch_size <= 0:
            raise ValueError("embedding_batch_size must be positive")
        self._connection_pool = connection_pool
        self._source_storage = source_storage
        self._parser = parser
        self._embedding_provider = embedding_provider
        self._arrival_service = arrival_service
        self._stale_claim_after = validate_stale_claim_after(stale_claim_after)
        self._embedding_batch_size = embedding_batch_size

    def ingest(self, command: IngestDocumentCommand) -> IngestDocumentResult:
        pile_id, logical_key = self._normalize(command)
        source = self._source_storage.publish(command.source)
        claimed = self._claim_or_replay(pile_id, logical_key, source)
        if isinstance(claimed, IngestDocumentResult):
            return claimed
        try:
            parsed = self._parser.parse_path(source.path)
            chunks = chunk_document(parsed)
            vectors = self._embed(tuple(chunk.content for chunk in chunks))
        except Exception as error:
            self._mark_failed(claimed)
            if isinstance(error, DocumentIngestionError):
                raise
            raise DocumentIngestionProcessingError("source parsing or embedding failed") from error
        result = self._persist_ready(claimed, parsed.mime_type, chunks, vectors)
        if self._arrival_service is not None:
            # Arrival is deliberately after READY commit.  A caller crash here is
            # repaired by the durable reconciler, not by pretending READY failed.
            self._arrival_service.reconcile_pile(pile_id)
        return result

    def _claim_or_replay(
        self,
        pile_id: str,
        logical_key: str,
        source: StoredSource,
    ) -> _Claim | IngestDocumentResult:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    claimed = self._claim_or_replay_once(connection, pile_id, logical_key, source)
                    connection.commit()
                    return claimed
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _claim_or_replay_once(
        self,
        connection: Connection,
        pile_id: str,
        logical_key: str,
        source: StoredSource,
    ) -> _Claim | IngestDocumentResult:
        pile = connection.execute("SELECT id FROM piles WHERE id = %s::uuid", (pile_id,)).fetchone()
        if pile is None:
            raise DocumentIngestionPileNotFoundError(f"pile {pile_id} does not exist")
        documents = DocumentRepository(connection)
        document = documents.create_or_get_for_update(pile_id, logical_key)
        versions = DocumentVersionRepository(connection)
        version = versions.get_by_document_and_content_hash(document.document_id, source.content_hash)
        key = self._key(pile_id, logical_key, source.content_hash)
        idempotency = IdempotencyRepository(connection)
        existing = idempotency.get(_SCOPE, key)
        if version is None:
            if existing is not None:
                raise DocumentIngestionIntegrityError(
                    "ingestion identity exists without its pending DocumentVersion",
                )
            version = versions.create(
                DocumentVersion(
                    document_version_id=str(uuid4()),
                    document_id=document.document_id,
                    version_number=versions.next_version_number(document.document_id),
                    content_hash=source.content_hash,
                    storage_uri=source.storage_uri,
                ),
                mime_type=None,
                size_bytes=source.size_bytes,
            )
            claim = idempotency.claim(_SCOPE, key)
            if claim is None:
                # The document/version insertion is in this same transaction;
                # rolling it back avoids an unowned PENDING Version.
                raise DocumentIngestionIntegrityError("ingestion claim raced version creation")
            return _Claim(pile_id, logical_key, document, version, source, key, claim.claim_generation)

        if version.storage_uri != source.storage_uri or version.size_bytes != source.size_bytes:
            raise DocumentIngestionIntegrityError("stored version does not match its content-addressed source")
        if version.processing_status is DocumentProcessingStatus.FAILED:
            raise DocumentIngestionPreviouslyFailedError("identical source previously failed ingestion")
        if existing is None:
            raise DocumentIngestionIntegrityError("DocumentVersion exists without an ingestion idempotency record")
        if version.processing_status is DocumentProcessingStatus.READY:
            return self._resolve_ready_replay(document, version, source, existing, key)
        if existing.status is IdempotencyStatus.COMPLETED:
            raise DocumentIngestionIntegrityError("completed ingestion left its version PENDING")
        if existing.status is IdempotencyStatus.FAILED:
            raise DocumentIngestionPreviouslyFailedError("document ingestion previously failed")
        classification = classify_in_progress(idempotency, existing, self._stale_claim_after)
        if classification is InProgressClaimState.MATCHING_FRESH:
            raise DocumentIngestionInProgressError("document ingestion is already in progress")
        if classification is InProgressClaimState.CHANGED_OR_MISSING:
            return self._resolve_authoritative_pending(document, version, source, key, idempotency)
        reclaimed = reclaim_if_stale(idempotency, existing, self._stale_claim_after)
        if reclaimed is None:
            return self._resolve_authoritative_pending(document, version, source, key, idempotency)
        return _Claim(pile_id, logical_key, document, version, source, key, reclaimed.claim_generation)

    def _resolve_ready_replay(
        self,
        document: DocumentRecord,
        version: DocumentVersionRecord,
        source: StoredSource,
        record: IdempotencyRecord,
        key: str,
    ) -> IngestDocumentResult:
        expected = self._reference(version.document_version_id, source.content_hash)
        if record.status is not IdempotencyStatus.COMPLETED:
            raise DocumentIngestionIntegrityError("READY version does not have a completed ingestion proof")
        if record.result_reference is None or not _REFERENCE.fullmatch(record.result_reference):
            raise DocumentIngestionIntegrityError("completed ingestion has malformed result reference")
        if record.result_reference != expected:
            raise DocumentIngestionInputConflictError("completed ingestion has a different result identity")
        if document.current_version_id != version.document_version_id:
            raise DocumentIngestionIntegrityError("READY ingestion version is not the current Document version")
        return IngestDocumentResult(
            document.document_id,
            version.document_version_id,
            source.content_hash,
            source.storage_uri,
            replayed=True,
        )

    def _resolve_authoritative_pending(
        self,
        document: DocumentRecord,
        version: DocumentVersionRecord,
        source: StoredSource,
        key: str,
        idempotency: IdempotencyRepository,
    ) -> _Claim | IngestDocumentResult:
        current = idempotency.get(_SCOPE, key)
        if current is None:
            raise DocumentIngestionIntegrityError("ingestion operation disappeared")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise DocumentIngestionInProgressError("document ingestion is already in progress")
        if current.status is IdempotencyStatus.FAILED:
            raise DocumentIngestionPreviouslyFailedError("document ingestion previously failed")
        if version.processing_status is DocumentProcessingStatus.READY:
            return self._resolve_ready_replay(document, version, source, current, key)
        raise DocumentIngestionIntegrityError("completed ingestion left DocumentVersion PENDING")

    def _embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for index in range(0, len(texts), self._embedding_batch_size):
            batch = texts[index:index + self._embedding_batch_size]
            try:
                values = self._embedding_provider.embed(batch)
            except EmbeddingProviderError:
                raise
            except Exception as error:
                raise EmbeddingProviderError("embedding provider failed") from error
            vectors.extend(validate_embeddings(batch, values))
        return tuple(vectors)

    def _persist_ready(
        self,
        claim: _Claim,
        mime_type: str,
        chunks: tuple[ParsedChunk, ...],
        vectors: tuple[tuple[float, ...], ...],
    ) -> IngestDocumentResult:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    documents = DocumentRepository(connection)
                    document = documents.get_for_update(claim.document.document_id)
                    version = DocumentVersionRepository(connection).get_for_update(claim.version.document_version_id)
                    if document is None or version is None:
                        raise DocumentIngestionIntegrityError("claimed DocumentVersion disappeared")
                    if (
                        version.processing_status is not DocumentProcessingStatus.PENDING
                        or version.content_hash != claim.source.content_hash
                        or version.storage_uri != claim.source.storage_uri
                    ):
                        raise DocumentIngestionIntegrityError("claimed DocumentVersion changed before READY persistence")
                    chunks_repository = DocumentChunkRepository(connection)
                    if chunks_repository.list_for_document_version(version.document_version_id):
                        raise DocumentIngestionIntegrityError("PENDING DocumentVersion already has chunks")
                    for parsed_chunk, vector in zip(chunks, vectors, strict=True):
                        chunks_repository.create(
                            version.document_version_id,
                            parsed_chunk.chunk_index,
                            parsed_chunk.content,
                            parsed_chunk.content_hash,
                            page_number=parsed_chunk.page_number,
                            embedding=vector,
                        )
                    versions = DocumentVersionRepository(connection)
                    if versions.update_metadata(
                        version.document_version_id,
                        mime_type=mime_type,
                        size_bytes=claim.source.size_bytes,
                    ) is None:
                        raise DocumentIngestionClaimLostError("DocumentVersion is no longer PENDING")
                    if versions.transition_from_pending(
                        version.document_version_id,
                        DocumentProcessingStatus.READY,
                    ) is None:
                        raise DocumentIngestionClaimLostError("DocumentVersion status ownership was lost")
                    if documents.set_current_version(document.document_id, version.document_version_id) is None:
                        raise DocumentIngestionIntegrityError("Document disappeared before current version update")
                    completed = IdempotencyRepository(connection).mark_completed(
                        _SCOPE,
                        claim.key,
                        claim.generation,
                        self._reference(version.document_version_id, claim.source.content_hash),
                    )
                    if completed is None:
                        raise DocumentIngestionClaimLostError("document ingestion claim is no longer owned")
                    connection.commit()
                    return IngestDocumentResult(
                        document.document_id,
                        version.document_version_id,
                        claim.source.content_hash,
                        claim.source.storage_uri,
                        replayed=False,
                    )
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _mark_failed(self, claim: _Claim) -> None:
        with self._connection_pool.connection() as connection:
            document = DocumentRepository(connection).get_for_update(claim.document.document_id)
            version = DocumentVersionRepository(connection).get_for_update(claim.version.document_version_id)
            if document is None or version is None:
                raise DocumentIngestionIntegrityError("claimed DocumentVersion disappeared")
            failed = IdempotencyRepository(connection).mark_failed(
                _SCOPE,
                claim.key,
                claim.generation,
            )
            if failed is None:
                raise DocumentIngestionClaimLostError("document ingestion failure ownership was lost")
            if DocumentVersionRepository(connection).transition_from_pending(
                version.document_version_id,
                DocumentProcessingStatus.FAILED,
            ) is None:
                raise DocumentIngestionClaimLostError("DocumentVersion was no longer PENDING")
            connection.commit()

    @staticmethod
    def _identity(pile_id: str, logical_key: str, content_hash: str) -> bytes:
        return json.dumps(
            {
                "content_hash": content_hash,
                "format": _SCOPE,
                "logical_key": logical_key,
                "pile_id": pile_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _key(cls, pile_id: str, logical_key: str, content_hash: str) -> str:
        return f"ingest:{pile_id}:{sha256(cls._identity(pile_id, logical_key, content_hash)).hexdigest()}"

    @staticmethod
    def _reference(document_version_id: str, content_hash: str) -> str:
        return f"document-ingestion:{document_version_id}:{content_hash}"

    @staticmethod
    def _normalize(command: IngestDocumentCommand) -> tuple[str, str]:
        try:
            pile_id = str(UUID(command.pile_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidDocumentIngestionCommandError("pile_id must be a valid UUID") from error
        logical_key = command.logical_key
        if (
            not isinstance(logical_key, str)
            or not logical_key
            or not logical_key.strip()
            or "\x00" in logical_key
            or "/" in logical_key
            or "\\" in logical_key
            or logical_key in {".", ".."}
        ):
            raise InvalidDocumentIngestionCommandError("logical_key must be a non-path Unicode identity")
        return pile_id, logical_key
