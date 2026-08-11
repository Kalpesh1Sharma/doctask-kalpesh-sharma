import pytest

from domain.document_version import (
    DocumentProcessingStatus,
    DocumentVersion,
    is_immutable_status,
    validate_processing_transition,
)


def make_version(
    status: DocumentProcessingStatus = DocumentProcessingStatus.PENDING,
) -> DocumentVersion:
    return DocumentVersion(
        document_version_id="version-1",
        document_id="document-1",
        version_number=1,
        content_hash="abc123",
        storage_uri="documents/document-1/version-1",
        processing_status=status,
    )


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (
            DocumentProcessingStatus.PENDING,
            DocumentProcessingStatus.READY,
        ),
        (
            DocumentProcessingStatus.PENDING,
            DocumentProcessingStatus.FAILED,
        ),
    ],
)
def test_valid_processing_transitions(
    current: DocumentProcessingStatus,
    requested: DocumentProcessingStatus,
) -> None:
    assert validate_processing_transition(current, requested) is True


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (
            DocumentProcessingStatus.READY,
            DocumentProcessingStatus.PENDING,
        ),
        (
            DocumentProcessingStatus.READY,
            DocumentProcessingStatus.FAILED,
        ),
        (
            DocumentProcessingStatus.FAILED,
            DocumentProcessingStatus.PENDING,
        ),
        (
            DocumentProcessingStatus.FAILED,
            DocumentProcessingStatus.READY,
        ),
    ],
)
def test_invalid_processing_transitions(
    current: DocumentProcessingStatus,
    requested: DocumentProcessingStatus,
) -> None:
    assert validate_processing_transition(current, requested) is False


@pytest.mark.parametrize(
    "status",
    list(DocumentProcessingStatus),
)
def test_same_status_is_idempotent(
    status: DocumentProcessingStatus,
) -> None:
    assert validate_processing_transition(status, status) is True


def test_ready_is_immutable() -> None:
    assert is_immutable_status(DocumentProcessingStatus.READY) is True


def test_failed_is_terminal_and_immutable() -> None:
    assert is_immutable_status(DocumentProcessingStatus.FAILED) is True


def test_pending_is_not_immutable() -> None:
    assert is_immutable_status(DocumentProcessingStatus.PENDING) is False


def test_default_status_is_pending() -> None:
    version = make_version()

    assert (
        version.processing_status
        == DocumentProcessingStatus.PENDING
    )


def test_document_version_is_immutable() -> None:
    version = make_version()

    with pytest.raises((TypeError, ValueError)):
        version.processing_status = DocumentProcessingStatus.READY