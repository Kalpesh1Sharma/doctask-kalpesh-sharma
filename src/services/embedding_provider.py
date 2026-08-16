"""Embedding provider boundary for document ingestion.

Embedding vectors accelerate retrieval only.  Provenance remains backed by the
immutable source/chunk/evidence relations and does not depend on a vector.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from hashlib import sha256
from typing import Protocol

EMBEDDING_DIMENSION = 1536
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"


class EmbeddingProviderError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Return exactly one finite 1536-dimensional vector per input text."""


def validate_embeddings(
    texts: Sequence[str],
    vectors: Sequence[Sequence[float]],
) -> tuple[tuple[float, ...], ...]:
    if len(texts) != len(vectors):
        raise EmbeddingProviderError("embedding provider returned an unexpected vector count")
    normalized: list[tuple[float, ...]] = []
    for vector in vectors:
        values = tuple(float(value) for value in vector)
        if len(values) != EMBEDDING_DIMENSION:
            raise EmbeddingProviderError("embedding provider returned a vector with the wrong dimension")
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingProviderError("embedding provider returned non-finite values")
        normalized.append(values)
    return tuple(normalized)


class DeterministicEmbeddingProvider:
    """Offline deterministic provider suitable for integration tests and local use."""

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            seed = sha256(text.encode("utf-8")).digest()
            values = tuple(
                ((seed[index % len(seed)] + index * 17) % 251) / 250.0
                for index in range(EMBEDDING_DIMENSION)
            )
            vectors.append(values)
        return validate_embeddings(texts, vectors)


class OpenAIEmbeddingProvider:
    """Synchronous OpenAI adapter; no semantic retries are performed here."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        model: str = OPENAI_EMBEDDING_MODEL,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._timeout_seconds = timeout_seconds

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            )
            response = client.embeddings.create(model=self._model, input=list(texts))
            vectors = tuple(tuple(item.embedding) for item in response.data)
        except Exception as error:  # Adapter maps transport/provider failures at its boundary.
            raise EmbeddingProviderError("OpenAI embedding request failed") from error
        return validate_embeddings(texts, vectors)
