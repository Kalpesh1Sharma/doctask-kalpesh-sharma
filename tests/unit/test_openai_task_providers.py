from uuid import uuid4

import pytest

from domain.contradiction_detection import ContradictionDetectionRequest
from domain.fact_extraction import FactExtractionRequest
from domain.rule_evaluation import RuleEvaluationRequest
from services.contradiction_detection_provider import (
    ContradictionDetectionInvalidResponseError,
)
from services.fact_extraction_provider import FactExtractionInvalidResponseError
from services.openai_structured_transport import ProviderUsage, StructuredTransportInvalidResponse
from services.openai_task_providers import (
    OpenAIContradictionDetectionProvider,
    OpenAIFactExtractionProvider,
    OpenAIRuleEvaluationProvider,
)
from services.rule_evaluation_provider import RuleEvaluationInvalidResponseError


class InvalidStructuredTransport:
    def parse(self, **_: object):
        raise StructuredTransportInvalidResponse("invalid structured output")


class InvalidStructuredTransportWithUsage:
    def parse(self, **_: object):
        raise StructuredTransportInvalidResponse(
            "invalid structured output",
            usage=ProviderUsage("openai", "resolved-model", 31, 37),
        )


def test_openai_fact_adapter_preserves_invalid_structured_output_classification() -> None:
    request = FactExtractionRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        document_version_ids=(),
        source_chunks=(),
    )

    with pytest.raises(FactExtractionInvalidResponseError):
        OpenAIFactExtractionProvider(InvalidStructuredTransport(), "test-model").extract(request)


def test_openai_contradiction_adapter_preserves_invalid_structured_output_classification() -> None:
    request = ContradictionDetectionRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        source_facts=(),
        candidate_pairs=(),
    )

    with pytest.raises(ContradictionDetectionInvalidResponseError):
        OpenAIContradictionDetectionProvider(InvalidStructuredTransport(), "test-model").detect(request)


def test_openai_rule_adapter_preserves_invalid_structured_output_classification() -> None:
    request = RuleEvaluationRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        rule_sets=(),
        rules=(),
        source_facts=(),
    )

    with pytest.raises(RuleEvaluationInvalidResponseError):
        OpenAIRuleEvaluationProvider(InvalidStructuredTransport(), "test-model").evaluate(request)


def test_invalid_structured_output_keeps_known_provider_usage() -> None:
    request = FactExtractionRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        document_version_ids=(),
        source_chunks=(),
    )
    provider = OpenAIFactExtractionProvider(InvalidStructuredTransportWithUsage(), "test-model")

    with pytest.raises(FactExtractionInvalidResponseError):
        provider.extract(request)

    assert provider.last_usage == ProviderUsage("openai", "resolved-model", 31, 37)
