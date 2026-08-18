"""Pure contracts for grounded semantic document-role classification."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CLASSIFICATION_FORMAT = "document-classification-v1"


class DocumentRole(StrEnum):
    LOAN_ESTIMATE = "LOAN_ESTIMATE"
    CLOSING_DISCLOSURE = "CLOSING_DISCLOSURE"
    PROMISSORY_NOTE = "PROMISSORY_NOTE"
    SETTLEMENT_STATEMENT = "SETTLEMENT_STATEMENT"
    UNDERWRITING_APPROVAL = "UNDERWRITING_APPROVAL"
    UNKNOWN = "UNKNOWN"


def _uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid UUID") from error


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class DocumentClassificationSourceChunk(BaseModel):
    """Provider-visible source material; ``content`` is always untrusted data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    chunk_index: int
    page_number: int | None = None
    content: str
    content_digest: str

    @field_validator("chunk_id")
    @classmethod
    def normalize_chunk_id(cls, value: str) -> str:
        return _uuid(value, "chunk_id")

    @model_validator(mode="after")
    def validate_chunk(self) -> DocumentClassificationSourceChunk:
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be nonnegative")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("page_number must be positive")
        if not self.content:
            raise ValueError("classification source content must not be empty")
        if self.content_digest != _sha256_text(self.content):
            raise ValueError("content_digest must match content")
        return self


class DocumentClassificationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["document-classification-v1"] = _CLASSIFICATION_FORMAT
    document_version_id: str
    instruction_version: str
    trusted_instructions: str
    source_chunks: tuple[DocumentClassificationSourceChunk, ...]

    @field_validator("document_version_id")
    @classmethod
    def normalize_document_version_id(cls, value: str) -> str:
        return _uuid(value, "document_version_id")

    @field_validator("instruction_version", "trusted_instructions")
    @classmethod
    def require_nonempty_text(cls, value: str, info) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{info.field_name} must be non-empty text")
        return value.strip()

    @field_validator("source_chunks")
    @classmethod
    def normalize_source_chunks(
        cls,
        chunks: tuple[DocumentClassificationSourceChunk, ...],
    ) -> tuple[DocumentClassificationSourceChunk, ...]:
        if not chunks:
            raise ValueError("source_chunks must not be empty")
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("source_chunks cannot contain duplicate chunk IDs")
        return tuple(sorted(chunks, key=lambda chunk: (chunk.chunk_index, chunk.chunk_id)))


class DocumentClassificationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["document-classification-v1"]
    role: DocumentRole
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    evidence_chunk_ids: tuple[str, ...] = ()
    explanation: str

    @field_validator("evidence_chunk_ids")
    @classmethod
    def normalize_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_uuid(value, "evidence_chunk_ids") for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("evidence_chunk_ids cannot contain duplicates")
        return normalized

    @field_validator("explanation")
    @classmethod
    def require_explanation(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("explanation must be non-empty text")
        return value.strip()

    @model_validator(mode="after")
    def require_grounding_for_known_role(self) -> DocumentClassificationResponse:
        if self.role is not DocumentRole.UNKNOWN and not self.evidence_chunk_ids:
            raise ValueError("a known document role requires supporting source chunks")
        return self


def document_classification_input_fingerprint(request: DocumentClassificationRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
