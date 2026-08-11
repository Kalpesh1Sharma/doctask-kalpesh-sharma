import pytest

from domain.evidence import EvidenceSpan


def make_evidence() -> EvidenceSpan:
    return EvidenceSpan(
        evidence_id="evidence-1",
        document_id="document-1",
        document_version_id="version-1",
        start_offset=100,
        end_offset=150,
        source_text="The loan amount is $100,000.",
        content_hash="abc123",
    )


def test_evidence_stores_source_identity() -> None:
    evidence = make_evidence()

    assert evidence.document_id == "document-1"
    assert evidence.document_version_id == "version-1"


def test_evidence_stores_span() -> None:
    evidence = make_evidence()

    assert evidence.start_offset == 100
    assert evidence.end_offset == 150


def test_evidence_stores_source_text() -> None:
    evidence = make_evidence()

    assert evidence.source_text == "The loan amount is $100,000."


def test_evidence_stores_content_hash() -> None:
    evidence = make_evidence()

    assert evidence.content_hash == "abc123"


@pytest.mark.parametrize(
    "field",
    [
        "evidence_id",
        "document_id",
        "document_version_id",
        "start_offset",
        "end_offset",
        "source_text",
        "content_hash",
    ],
)
def test_evidence_is_immutable(field: str) -> None:
    evidence = make_evidence()

    with pytest.raises((TypeError, ValueError)):
        setattr(evidence, field, "changed")