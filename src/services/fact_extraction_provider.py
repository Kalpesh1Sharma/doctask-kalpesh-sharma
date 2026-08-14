"""Provider-neutral fact extraction boundary."""

from typing import Protocol

from domain.fact_extraction import FactExtractionRequest, FactExtractionResponse


class FactExtractionProvider(Protocol):
    def extract(self, request: FactExtractionRequest) -> FactExtractionResponse:
        """Return only a validated structured extraction response."""


class FactExtractionProviderError(RuntimeError):
    """Raised when a provider cannot complete a structured extraction request."""
