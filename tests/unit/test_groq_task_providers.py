from uuid import uuid4

import pytest

from domain.contradiction_detection import ContradictionDetectionRequest
from domain.fact_extraction import FactExtractionRequest
from domain.rule_evaluation import RuleEvaluationRequest
from services.contradiction_detection_provider import ContradictionDetectionInvalidResponseError
from services.fact_extraction_provider import FactExtractionInvalidResponseError
from services.groq_structured_transport import GroqStructuredTransportInvalidResponse
from services.groq_task_providers import (
    GroqContradictionDetectionProvider,
    GroqFactExtractionProvider,
    GroqRuleEvaluationProvider,
)
from services.openai_structured_transport import ProviderUsage
from services.rule_evaluation_provider import RuleEvaluationInvalidResponseError


class InvalidStructuredTransport:
    def parse(self, **_: object):
        raise GroqStructuredTransportInvalidResponse(
            "invalid structured output",
            usage=ProviderUsage("groq", "resolved-model", 31, 37),
        )


def test_groq_fact_adapter_preserves_invalid_structured_output_classification_and_usage() -> None:
    request = FactExtractionRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        document_version_ids=(),
        source_chunks=(),
    )
    provider = GroqFactExtractionProvider(InvalidStructuredTransport(), "test-model")

    with pytest.raises(FactExtractionInvalidResponseError):
        provider.extract(request)

    assert provider.provider_name == "groq"
    assert provider.last_usage == ProviderUsage("groq", "resolved-model", 31, 37)


def test_groq_contradiction_adapter_preserves_invalid_structured_output_classification() -> None:
    request = ContradictionDetectionRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        source_facts=(),
        candidate_pairs=(),
    )

    with pytest.raises(ContradictionDetectionInvalidResponseError):
        GroqContradictionDetectionProvider(
            InvalidStructuredTransport(),
            "test-model",
        ).detect(request)


def test_groq_rule_adapter_preserves_invalid_structured_output_classification() -> None:
    request = RuleEvaluationRequest(
        run_id=str(uuid4()),
        instruction_version="v1",
        trusted_instructions="trusted",
        rule_sets=(),
        rules=(),
        source_facts=(),
    )

    with pytest.raises(RuleEvaluationInvalidResponseError):
        GroqRuleEvaluationProvider(InvalidStructuredTransport(), "test-model").evaluate(request)
