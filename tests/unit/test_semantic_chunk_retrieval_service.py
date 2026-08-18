from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from api.application.semantic_chunk_retrieval_service import (
    SemanticChunkRetrievalIntegrityError,
    SemanticChunkRetrievalService,
)
from repositories.document_chunk_repository import ChunkSearchResult, DocumentChunkRecord


class _Embedding:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed(self, texts):
        self.calls.append(tuple(texts))
        return ((1.0,) * 1536,)


class _Repository:
    chunks: ClassVar[list[DocumentChunkRecord]] = []
    results: ClassVar[list[ChunkSearchResult]] = []
    search_calls: ClassVar[
        list[tuple[tuple[float, ...], tuple[str, ...], int]]
    ] = []

    def __init__(self, _connection) -> None:
        pass

    def list_for_document_version(self, document_version_id: str):
        return [item for item in self.chunks if item.document_version_id == document_version_id]

    def list_for_document_version_for_share(self, document_version_id: str):
        return self.list_for_document_version(document_version_id)

    def search_similar(self, query_embedding, document_version_ids, limit):
        self.search_calls.append((tuple(query_embedding), tuple(document_version_ids), limit))
        return self.results[:limit]


def _chunk(chunk_id: str, version_id: str, index: int, *, embedded: bool = True):
    return DocumentChunkRecord(
        chunk_id=chunk_id,
        document_version_id=version_id,
        chunk_index=index,
        content=f"chunk {index}",
        content_hash=f"sha256:{index:064x}",
        page_number=None,
        start_offset=0,
        end_offset=7,
        embedding=((0.0,) * 1536) if embedded else None,
        created_at=datetime.now(UTC),
    )


def test_retrieval_uses_pgvector_ranking_and_preserves_authoritative_chunks(monkeypatch) -> None:
    version_id = "11111111-1111-4111-8111-111111111111"
    first = _chunk("22222222-2222-4222-8222-222222222222", version_id, 0)
    second = _chunk("33333333-3333-4333-8333-333333333333", version_id, 1)
    _Repository.chunks = [first, second]
    _Repository.results = [
        ChunkSearchResult(replace(second, content="repository copy should not escape"), 0.01),
        ChunkSearchResult(first, 0.20),
    ]
    _Repository.search_calls = []
    monkeypatch.setattr(
        "api.application.semantic_chunk_retrieval_service.DocumentChunkRepository",
        _Repository,
    )
    embedding = _Embedding()

    result = SemanticChunkRetrievalService(embedding, default_limit=1).retrieve(
        object(),
        (version_id,),
        "payment obligations",
    )

    assert result == (second,)
    assert embedding.calls == [("payment obligations",)]
    assert len(_Repository.search_calls) == 1
    _, version_ids, limit = _Repository.search_calls[0]
    assert version_ids == (version_id,)
    assert limit == 1


def test_retrieval_fails_closed_when_a_source_chunk_has_no_embedding(monkeypatch) -> None:
    version_id = "11111111-1111-4111-8111-111111111111"
    _Repository.chunks = [
        _chunk("22222222-2222-4222-8222-222222222222", version_id, 0, embedded=False),
    ]
    _Repository.results = []
    monkeypatch.setattr(
        "api.application.semantic_chunk_retrieval_service.DocumentChunkRepository",
        _Repository,
    )

    with pytest.raises(SemanticChunkRetrievalIntegrityError, match="without embeddings"):
        SemanticChunkRetrievalService(_Embedding()).retrieve(
            object(),
            (version_id,),
            "material facts",
        )
