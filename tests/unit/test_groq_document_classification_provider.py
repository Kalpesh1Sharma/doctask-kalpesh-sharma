from __future__ import annotations

import pytest

from domain.document_classification import (
    DocumentClassificationRequest,
    DocumentClassificationResponse,
    DocumentClassificationSourceChunk,
    DocumentRole,
)
from services.document_classification_provider import (
    DocumentClassificationProviderError,
    DocumentClassificationProviderInvalidResponseError,
)
from services.groq_document_classification_provider import (
    GroqDocumentClassificationProvider,
)
from services.groq_structured_transport import (
    GroqStructuredTransportError,
    GroqStructuredTransportInvalidResponse,
)
from services.openai_structured_transport import ProviderUsage

_REQUEST = DocumentClassificationRequest(
    document_version_id="11111111-1111-4111-8111-111111111111",
    instruction_version="test-v1",
    trusted_instructions="Classify only from grounded source text.",
    source_chunks=(
        DocumentClassificationSourceChunk(
            chunk_id="22222222-2222-4222-8222-222222222222",
            chunk_index=0,
            content="Closing Disclosure",
            content_digest="sha256:856261bd754c79d4204a654a11e7208ea67ddfe1918621bff9069fc563a6f23d",
        ),
    ),
)


class _Transport:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def test_groq_classification_provider_preserves_structured_contract_and_usage() -> None:
    usage = ProviderUsage("groq", "openai/gpt-oss-20b", 12, 4)
    response = DocumentClassificationResponse(
        format="document-classification-v1",
        role=DocumentRole.CLOSING_DISCLOSURE,
        confidence=0.98,
        evidence_chunk_ids=("22222222-2222-4222-8222-222222222222",),
        explanation="The source identifies itself as a Closing Disclosure.",
    )
    transport = _Transport((response, usage))
    provider = GroqDocumentClassificationProvider(transport, "openai/gpt-oss-20b")  # type: ignore[arg-type]

    assert provider.classify(_REQUEST) == response
    assert provider.last_usage == usage
    assert transport.calls[0]["trusted_instructions"] == _REQUEST.trusted_instructions
    assert transport.calls[0]["untrusted_input"] is _REQUEST


def test_groq_classification_provider_maps_invalid_structured_output() -> None:
    usage = ProviderUsage("groq", "openai/gpt-oss-20b", 12, 4)
    transport = _Transport(error=GroqStructuredTransportInvalidResponse("bad", usage=usage))
    provider = GroqDocumentClassificationProvider(transport, "openai/gpt-oss-20b")  # type: ignore[arg-type]

    with pytest.raises(DocumentClassificationProviderInvalidResponseError):
        provider.classify(_REQUEST)
    assert provider.last_usage == usage


def test_groq_classification_provider_maps_transport_failure() -> None:
    transport = _Transport(error=GroqStructuredTransportError("down"))
    provider = GroqDocumentClassificationProvider(transport, "openai/gpt-oss-20b")  # type: ignore[arg-type]

    with pytest.raises(DocumentClassificationProviderError):
        provider.classify(_REQUEST)
