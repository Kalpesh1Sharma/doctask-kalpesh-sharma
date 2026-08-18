"""Provider boundary for semantic document-role classification."""

from __future__ import annotations

from typing import Protocol

from domain.document_classification import (
    DocumentClassificationRequest,
    DocumentClassificationResponse,
)


class DocumentClassificationProviderError(RuntimeError):
    pass


class DocumentClassificationProviderInvalidResponseError(DocumentClassificationProviderError):
    pass


class DocumentClassificationProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def last_usage(self) -> object | None: ...

    def classify(self, request: DocumentClassificationRequest) -> DocumentClassificationResponse: ...
