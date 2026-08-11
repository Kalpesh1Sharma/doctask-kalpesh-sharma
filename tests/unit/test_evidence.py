import pytest
from pydantic import ValidationError

from domain.evidence import EvidenceSpan


def make_evidence(**overrides: object) -> EvidenceSpan:
    values: dict[str, object] = {
        "evidence_id": "evidence-1",
        "document_version_id": "version-1",
        "chunk_id": "chunk-1",
        "start_offset": 100,
        "end_offset": 150,
        "source_text": "The loan amount is $100,000.",
        "content_hash": "abc123",
    }
    values.update(overrides)
    return EvidenceSpan(**values)


def test_evidence_stores_chunk_and_document_version_provenance() -> None:
    evidence = make_evidence()

    assert evidence.document_version_id == "version-1"
    assert evidence.chunk_id == "chunk-1"
    assert evidence.source_text == "The loan amount is $100,000."
    assert evidence.content_hash == "abc123"


def test_evidence_supports_page_only_location() -> None:
    evidence = make_evidence(
        page_number=2,
        start_offset=None,
        end_offset=None,
    )

    assert evidence.page_number == 2
    assert evidence.start_offset is None
    assert evidence.end_offset is None


def test_evidence_supports_nullable_offsets() -> None:
    evidence = make_evidence(start_offset=None, end_offset=None)

    assert evidence.start_offset is None
    assert evidence.end_offset is None


def test_evidence_stores_valid_offsets() -> None:
    evidence = make_evidence(start_offset=100, end_offset=150)

    assert evidence.start_offset == 100
    assert evidence.end_offset == 150


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("page_number", -1),
        ("start_offset", -1),
        ("end_offset", -1),
    ],
)
def test_evidence_rejects_negative_location_values(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_evidence(**{field: value})


def test_evidence_rejects_end_offset_before_start_offset() -> None:
    with pytest.raises(ValidationError):
        make_evidence(start_offset=150, end_offset=100)


def test_evidence_is_immutable() -> None:
    evidence = make_evidence()

    with pytest.raises(ValidationError):
        evidence.chunk_id = "chunk-2"
