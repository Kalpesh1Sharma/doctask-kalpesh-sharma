"""Pure contracts for grounded document fact extraction."""

import hashlib
import json
from math import isfinite
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, field_validator, model_validator

_EXTRACTION_FORMAT = "fact-extraction-v1"


def normalize_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid UUID") from error


def sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _contains_non_finite_number(value: object) -> bool:
    if isinstance(value, float):
        return not isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite_number(item) for item in value)
    return False


class FactExtractionSourceChunk(BaseModel):
    """A provider-visible chunk whose content is untrusted source data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_version_id: str
    chunk_index: int
    page_number: int | None = None
    content: str
    content_digest: str

    @field_validator("chunk_id", "document_version_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @model_validator(mode="after")
    def validate_location_and_digest(self) -> "FactExtractionSourceChunk":
        if self.chunk_index < 0:
            raise ValueError("chunk_index must be nonnegative")
        if self.page_number is not None and self.page_number <= 0:
            raise ValueError("page_number must be positive")
        if self.content_digest != sha256_text(self.content):
            raise ValueError("content_digest must match content")
        return self


class FactEvidenceCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    relative_start_offset: int
    relative_end_offset: int
    quoted_text: str

    @field_validator("chunk_id")
    @classmethod
    def normalize_chunk_id(cls, value: str) -> str:
        return normalize_uuid(value, "chunk_id")

    @model_validator(mode="after")
    def validate_relative_location(self) -> "FactEvidenceCandidate":
        if self.relative_start_offset < 0:
            raise ValueError("relative_start_offset must be nonnegative")
        if self.relative_end_offset <= self.relative_start_offset:
            raise ValueError("relative_end_offset must follow relative_start_offset")
        if not self.quoted_text:
            raise ValueError("quoted_text must not be empty")
        return self


class FactExtractionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    predicate: str
    object_value: JsonValue
    evidence: tuple[FactEvidenceCandidate, ...]

    @model_validator(mode="after")
    def validate_candidate(self) -> "FactExtractionCandidate":
        if not self.subject:
            raise ValueError("subject must not be empty")
        if not self.predicate:
            raise ValueError("predicate must not be empty")
        if not self.evidence:
            raise ValueError("a fact candidate requires evidence")
        if _contains_non_finite_number(self.object_value):
            raise ValueError("object_value must not contain non-finite numbers")
        return self


class FactExtractionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["fact-extraction-v1"]
    candidates: tuple[FactExtractionCandidate, ...]


class FactExtractionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["fact-extraction-v1"] = _EXTRACTION_FORMAT
    run_id: str
    instruction_version: str
    trusted_instructions: str
    document_version_ids: tuple[str, ...]
    source_chunks: tuple[FactExtractionSourceChunk, ...]

    @field_validator("run_id")
    @classmethod
    def normalize_run_id(cls, value: str) -> str:
        return normalize_uuid(value, "run_id")

    @field_validator("document_version_ids")
    @classmethod
    def normalize_document_version_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized_values = tuple(
            normalize_uuid(value, "document_version_ids") for value in values
        )
        if len(set(normalized_values)) != len(normalized_values):
            raise ValueError("document_version_ids cannot contain duplicates")
        return tuple(sorted(normalized_values))

    @field_validator("source_chunks")
    @classmethod
    def normalize_source_order(
        cls,
        source_chunks: tuple[FactExtractionSourceChunk, ...],
    ) -> tuple[FactExtractionSourceChunk, ...]:
        chunk_ids = [chunk.chunk_id for chunk in source_chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("source_chunks cannot contain duplicate chunk IDs")

        return tuple(
            sorted(
                source_chunks,
                key=lambda chunk: (
                    chunk.document_version_id,
                    chunk.chunk_index,
                    chunk.chunk_id,
                ),
            ),
        )

    @model_validator(mode="after")
    def validate_source_document_scope(self) -> "FactExtractionRequest":
        document_version_ids = set(self.document_version_ids)
        if any(
            chunk.document_version_id not in document_version_ids
            for chunk in self.source_chunks
        ):
            raise ValueError("source chunks must belong to document_version_ids")
        return self


def canonical_fact_extraction_input_bytes(request: FactExtractionRequest) -> bytes:
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def fact_extraction_input_fingerprint(request: FactExtractionRequest) -> str:
    return f"sha256:{hashlib.sha256(canonical_fact_extraction_input_bytes(request)).hexdigest()}"


def fact_candidate_key(candidate: FactExtractionCandidate) -> str:
    return json.dumps(
        [candidate.subject, candidate.predicate, candidate.object_value],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
