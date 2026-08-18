"""Groq adapter for grounded semantic document-role classification."""

from __future__ import annotations

from domain.document_classification import (
    DocumentClassificationRequest,
    DocumentClassificationResponse,
)
from services.document_classification_provider import (
    DocumentClassificationProviderError,
    DocumentClassificationProviderInvalidResponseError,
)
from services.groq_structured_transport import (
    GroqStructuredTransport,
    GroqStructuredTransportError,
    GroqStructuredTransportInvalidResponse,
)
from services.openai_structured_transport import ProviderUsage


class GroqDocumentClassificationProvider:
    def __init__(self, transport: GroqStructuredTransport, model: str) -> None:
        self._transport = transport
        self._model = model
        self._last_usage: ProviderUsage | None = None

    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def last_usage(self) -> ProviderUsage | None:
        return self._last_usage

    def classify(self, request: DocumentClassificationRequest) -> DocumentClassificationResponse:
        try:
            self._last_usage = None
            response, self._last_usage = self._transport.parse(
                model=self._model,
                trusted_instructions=request.trusted_instructions,
                untrusted_input=request,
                response_model=DocumentClassificationResponse,
            )
            return response
        except GroqStructuredTransportInvalidResponse as error:
            self._last_usage = error.usage
            raise DocumentClassificationProviderInvalidResponseError(
                "document classification returned invalid structured output",
            ) from error
        except GroqStructuredTransportError as error:
            raise DocumentClassificationProviderError(
                "document classification provider failed",
            ) from error
