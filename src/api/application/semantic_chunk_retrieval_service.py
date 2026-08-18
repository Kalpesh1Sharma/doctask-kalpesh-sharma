"""Grounded pgvector retrieval for provider-visible analysis context."""

from __future__ import annotations

from collections.abc import Sequence

from psycopg import Connection

from repositories.document_chunk_repository import (
    DocumentChunkRecord,
    DocumentChunkRepository,
)
from services.embedding_provider import EmbeddingProvider, EmbeddingProviderError


class SemanticChunkRetrievalError(Exception):
    """Base error for vector-retrieval preparation."""


class SemanticChunkRetrievalIntegrityError(SemanticChunkRetrievalError):
    """Durable chunk state cannot support the configured retrieval path."""


class SemanticChunkRetrievalProviderError(SemanticChunkRetrievalError):
    """The local/query embedding provider failed."""


class SemanticChunkRetrievalService:
    """Select ranked source chunks with PostgreSQL/pgvector cosine search.

    Embeddings are only a retrieval accelerator. The returned chunks remain the
    immutable source records used by the existing evidence-validation layer, so
    a provider still cannot cite data outside the retrieved request scope.
    """

    def __init__(self, embedding_provider: EmbeddingProvider, *, default_limit: int = 12) -> None:
        if default_limit <= 0:
            raise ValueError("default_limit must be positive")
        self._embedding_provider = embedding_provider
        self._default_limit = default_limit

    def retrieve(
        self,
        connection: Connection,
        document_version_ids: Sequence[str],
        query_text: str,
        *,
        limit: int | None = None,
        for_share: bool = False,
    ) -> tuple[DocumentChunkRecord, ...]:
        if not query_text.strip():
            raise ValueError("query_text must not be empty")
        if not document_version_ids:
            return ()
        requested_limit = self._default_limit if limit is None else limit
        if requested_limit <= 0:
            raise ValueError("limit must be positive")

        repository = DocumentChunkRepository(connection)
        source_chunks: list[DocumentChunkRecord] = []
        for version_id in document_version_ids:
            if for_share:
                source_chunks.extend(repository.list_for_document_version_for_share(version_id))
            else:
                source_chunks.extend(repository.list_for_document_version(version_id))
        if not source_chunks:
            raise SemanticChunkRetrievalIntegrityError(
                "analysis scope contains no durable document chunks",
            )
        missing_embeddings = tuple(
            sorted(chunk.chunk_id for chunk in source_chunks if chunk.embedding is None)
        )
        if missing_embeddings:
            raise SemanticChunkRetrievalIntegrityError(
                "analysis scope contains chunks without embeddings: "
                f"{missing_embeddings}",
            )

        try:
            vectors = self._embedding_provider.embed((query_text,))
        except EmbeddingProviderError as error:
            raise SemanticChunkRetrievalProviderError(
                "query embedding failed",
            ) from error
        if len(vectors) != 1:
            raise SemanticChunkRetrievalProviderError(
                "query embedding provider returned an unexpected vector count",
            )

        results = repository.search_similar(
            vectors[0],
            tuple(document_version_ids),
            min(requested_limit, len(source_chunks)),
        )
        if not results:
            raise SemanticChunkRetrievalIntegrityError(
                "pgvector retrieval returned no chunks for a non-empty analysis scope",
            )
        source_by_id = {chunk.chunk_id: chunk for chunk in source_chunks}
        ranked: list[DocumentChunkRecord] = []
        for result in results:
            authoritative = source_by_id.get(result.chunk.chunk_id)
            if authoritative is None:
                raise SemanticChunkRetrievalIntegrityError(
                    "pgvector retrieval escaped the requested document scope",
                )
            ranked.append(authoritative)
        return tuple(ranked)
