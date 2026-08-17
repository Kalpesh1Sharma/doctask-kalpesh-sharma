"""Production Groq adapters for the immutable semantic provider Protocols."""

from __future__ import annotations

from domain.contradiction_detection import (
    ContradictionDetectionRequest,
    ContradictionDetectionResponse,
)
from domain.fact_extraction import (
    FactExtractionRequest,
    FactExtractionResponse,
    UpdateFactExtractionRequest,
)
from domain.rule_evaluation import RuleEvaluationRequest, RuleEvaluationResponse
from services.contradiction_detection_provider import (
    ContradictionDetectionInvalidResponseError,
    ContradictionDetectionProviderError,
)
from services.fact_extraction_provider import (
    FactExtractionInvalidResponseError,
    FactExtractionProviderError,
)
from services.groq_structured_transport import (
    GroqStructuredTransport,
    GroqStructuredTransportError,
    GroqStructuredTransportInvalidResponse,
)
from services.openai_structured_transport import ProviderUsage
from services.rule_evaluation_provider import (
    RuleEvaluationInvalidResponseError,
    RuleEvaluationProviderError,
)


class GroqFactExtractionProvider:
    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model

    def __init__(self, transport: GroqStructuredTransport, model: str) -> None:
        self._transport = transport
        self._model = model
        self.last_usage: ProviderUsage | None = None

    def extract(
        self,
        request: FactExtractionRequest | UpdateFactExtractionRequest,
    ) -> FactExtractionResponse:
        try:
            self.last_usage = None
            response, self.last_usage = self._transport.parse(
                model=self._model,
                trusted_instructions=request.trusted_instructions,
                untrusted_input=request,
                response_model=FactExtractionResponse,
            )
            return response
        except GroqStructuredTransportInvalidResponse as error:
            self.last_usage = error.usage
            raise FactExtractionInvalidResponseError(
                "fact extraction returned invalid structured output",
            ) from error
        except GroqStructuredTransportError as error:
            raise FactExtractionProviderError("fact extraction provider failed") from error


class GroqContradictionDetectionProvider:
    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model

    def __init__(self, transport: GroqStructuredTransport, model: str) -> None:
        self._transport = transport
        self._model = model
        self.last_usage: ProviderUsage | None = None

    def detect(self, request: ContradictionDetectionRequest) -> ContradictionDetectionResponse:
        try:
            self.last_usage = None
            response, self.last_usage = self._transport.parse(
                model=self._model,
                trusted_instructions=request.trusted_instructions,
                untrusted_input=request,
                response_model=ContradictionDetectionResponse,
            )
            return response
        except GroqStructuredTransportInvalidResponse as error:
            self.last_usage = error.usage
            raise ContradictionDetectionInvalidResponseError(
                "contradiction detection returned invalid structured output",
            ) from error
        except GroqStructuredTransportError as error:
            raise ContradictionDetectionProviderError(
                "contradiction detection provider failed",
            ) from error


class GroqRuleEvaluationProvider:
    @property
    def provider_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model

    def __init__(self, transport: GroqStructuredTransport, model: str) -> None:
        self._transport = transport
        self._model = model
        self.last_usage: ProviderUsage | None = None

    def evaluate(self, request: RuleEvaluationRequest) -> RuleEvaluationResponse:
        try:
            self.last_usage = None
            response, self.last_usage = self._transport.parse(
                model=self._model,
                trusted_instructions=request.trusted_instructions,
                untrusted_input=request,
                response_model=RuleEvaluationResponse,
            )
            return response
        except GroqStructuredTransportInvalidResponse as error:
            self.last_usage = error.usage
            raise RuleEvaluationInvalidResponseError(
                "rule evaluation returned invalid structured output",
            ) from error
        except GroqStructuredTransportError as error:
            raise RuleEvaluationProviderError("rule evaluation provider failed") from error
