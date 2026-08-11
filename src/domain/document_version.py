from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class DocumentProcessingStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"


class DocumentVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_version_id: str
    document_id: str
    version_number: int
    content_hash: str
    storage_uri: str
    processing_status: DocumentProcessingStatus = (
        DocumentProcessingStatus.PENDING
    )


VALID_PROCESSING_TRANSITIONS: dict[
    DocumentProcessingStatus,
    set[DocumentProcessingStatus],
] = {
    DocumentProcessingStatus.PENDING: {
        DocumentProcessingStatus.READY,
        DocumentProcessingStatus.FAILED,
    },
    DocumentProcessingStatus.READY: set(),
    DocumentProcessingStatus.FAILED: set(),
}


def validate_processing_transition(
    current: DocumentProcessingStatus,
    requested: DocumentProcessingStatus,
) -> bool:
    """Return whether a document-version status transition is valid."""

    if current == requested:
        return True

    return requested in VALID_PROCESSING_TRANSITIONS[current]


def is_immutable_status(
    status: DocumentProcessingStatus,
) -> bool:
    """Return whether the version is immutable."""

    return status in {
        DocumentProcessingStatus.READY,
        DocumentProcessingStatus.FAILED,
    }