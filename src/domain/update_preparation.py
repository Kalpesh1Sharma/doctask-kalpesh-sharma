"""Pure deterministic contracts for UPDATE Run preparation."""

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from domain.fact_extraction import normalize_uuid


class UpdateDocumentChangeKind(StrEnum):
    RETAINED = "RETAINED"
    REPLACED = "REPLACED"
    ADDED = "ADDED"


class UpdateDocumentChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    document_id: str
    kind: UpdateDocumentChangeKind
    baseline_document_version_id: str | None
    target_document_version_id: str
    target_version_number: int
    target_content_hash: str

    @field_validator(
        "document_id",
        "baseline_document_version_id",
        "target_document_version_id",
    )
    @classmethod
    def normalize_identifiers(cls, value: str | None, info) -> str | None:
        return None if value is None else normalize_uuid(value, info.field_name)

    @model_validator(mode="after")
    def validate_change_shape(self) -> "UpdateDocumentChange":
        if self.target_version_number <= 0:
            raise ValueError("target_version_number must be positive")
        if self.kind is UpdateDocumentChangeKind.ADDED:
            if self.baseline_document_version_id is not None:
                raise ValueError("ADDED changes cannot have a baseline version")
        elif self.baseline_document_version_id is None:
            raise ValueError("RETAINED and REPLACED changes require a baseline version")
        elif (
            self.kind is UpdateDocumentChangeKind.RETAINED
            and self.baseline_document_version_id != self.target_document_version_id
        ):
            raise ValueError("RETAINED changes must keep the baseline version")
        elif (
            self.kind is UpdateDocumentChangeKind.REPLACED
            and self.baseline_document_version_id == self.target_document_version_id
        ):
            raise ValueError("REPLACED changes must use a distinct target version")
        return self


class UpdatePreparationManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["update-preparation-v1"] = "update-preparation-v1"
    run_id: str
    baseline_run_id: str
    trigger_document_version_id: str
    document_changes: tuple[UpdateDocumentChange, ...]
    carried_fact_ids: tuple[str, ...]

    @field_validator("run_id", "baseline_run_id", "trigger_document_version_id")
    @classmethod
    def normalize_run_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @field_validator("document_changes")
    @classmethod
    def normalize_changes(
        cls,
        values: tuple[UpdateDocumentChange, ...],
    ) -> tuple[UpdateDocumentChange, ...]:
        ids = [value.document_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("document_changes cannot contain duplicate document IDs")
        return tuple(sorted(values, key=lambda value: value.document_id))

    @field_validator("carried_fact_ids")
    @classmethod
    def normalize_carried_facts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_uuid(value, "carried_fact_ids") for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("carried_fact_ids cannot contain duplicates")
        return tuple(sorted(normalized))


def canonical_update_preparation_bytes(manifest: UpdatePreparationManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def update_preparation_fingerprint(manifest: UpdatePreparationManifest) -> str:
    return f"sha256:{hashlib.sha256(canonical_update_preparation_bytes(manifest)).hexdigest()}"
