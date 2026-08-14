"""Provider-neutral contradiction-detection boundary."""

from typing import Protocol

from domain.contradiction_detection import (
    ContradictionDetectionRequest,
    ContradictionDetectionResponse,
)


class ContradictionDetectionProvider(Protocol):
    def detect(self, request: ContradictionDetectionRequest) -> ContradictionDetectionResponse:
        """Return only validated structured contradiction pairs."""


class ContradictionDetectionProviderError(RuntimeError):
    """Raised when a provider cannot complete a structured detection request."""
