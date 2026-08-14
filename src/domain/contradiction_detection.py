"""Pure contracts for grounded Run-level contradiction detection."""

import hashlib
import json
from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from domain.fact import FactStatus
from domain.fact_extraction import normalize_uuid
from domain.run_membership import RunMembershipRelation

_FORMAT = "contradiction-detection-v1"


def _contains_non_finite_number(value: object) -> bool:
    if isinstance(value, float):
        return not isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite_number(item) for item in value)
    return False


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


def canonical_json_value(value: object) -> str:
    """Return a stable JSON representation for a JSON-compatible value."""
    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class ContradictionSourceEvidence(BaseModel):
    """Exact durable evidence supplied to the detector as untrusted source data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    document_version_id: str
    chunk_id: str
    page_number: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    source_text: str
    content_hash: str

    @field_validator("evidence_id", "document_version_id", "chunk_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)


class ContradictionSourceFact(BaseModel):
    """A current-Run Fact and all durable evidence supporting it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    origin_run_id: str
    relation: RunMembershipRelation
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus
    evidence: tuple[ContradictionSourceEvidence, ...]

    @field_validator("fact_id", "origin_run_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @field_validator("object_value")
    @classmethod
    def validate_and_freeze_object_value(cls, value: JsonValue) -> object:
        if _contains_non_finite_number(value):
            raise ValueError("object_value must not contain non-finite numbers")
        return _freeze_json(value)

    @field_serializer("object_value")
    def serialize_object_value(self, value: object) -> JsonValue:
        return _thaw_json(value)

    @field_validator("evidence")
    @classmethod
    def normalize_evidence(
        cls,
        evidence: tuple[ContradictionSourceEvidence, ...],
    ) -> tuple[ContradictionSourceEvidence, ...]:
        evidence_ids = [item.evidence_id for item in evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("evidence cannot contain duplicate evidence IDs")
        return tuple(sorted(evidence, key=lambda item: item.evidence_id))


class ContradictionCandidatePair(BaseModel):
    """An unordered pair represented canonically by ordered Fact IDs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_a_id: str
    fact_b_id: str

    @field_validator("fact_a_id", "fact_b_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @model_validator(mode="after")
    def canonicalize_pair(self) -> "ContradictionCandidatePair":
        if self.fact_a_id == self.fact_b_id:
            raise ValueError("a contradiction pair requires two distinct Facts")
        if self.fact_a_id > self.fact_b_id:
            fact_a_id, fact_b_id = self.fact_b_id, self.fact_a_id
            object.__setattr__(self, "fact_a_id", fact_a_id)
            object.__setattr__(self, "fact_b_id", fact_b_id)
        return self

    @property
    def identity(self) -> tuple[str, str]:
        return self.fact_a_id, self.fact_b_id


class ContradictionDetectionResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["contradiction-detection-v1"]
    contradictions: tuple[ContradictionCandidatePair, ...]

    @field_validator("contradictions")
    @classmethod
    def deduplicate_pairs(
        cls,
        pairs: tuple[ContradictionCandidatePair, ...],
    ) -> tuple[ContradictionCandidatePair, ...]:
        unique = {pair.identity: pair for pair in pairs}
        return tuple(unique[identity] for identity in sorted(unique))


class ContradictionDetectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["contradiction-detection-v1"] = _FORMAT
    run_id: str
    instruction_version: str
    trusted_instructions: str
    source_facts: tuple[ContradictionSourceFact, ...]
    candidate_pairs: tuple[ContradictionCandidatePair, ...]

    @field_validator("run_id")
    @classmethod
    def normalize_run_id(cls, value: str) -> str:
        return normalize_uuid(value, "run_id")

    @field_validator("source_facts")
    @classmethod
    def normalize_source_facts(
        cls,
        facts: tuple[ContradictionSourceFact, ...],
    ) -> tuple[ContradictionSourceFact, ...]:
        fact_ids = [fact.fact_id for fact in facts]
        if len(set(fact_ids)) != len(fact_ids):
            raise ValueError("source_facts cannot contain duplicate Fact IDs")
        return tuple(sorted(facts, key=lambda fact: fact.fact_id))

    @field_validator("candidate_pairs")
    @classmethod
    def normalize_candidate_pairs(
        cls,
        pairs: tuple[ContradictionCandidatePair, ...],
    ) -> tuple[ContradictionCandidatePair, ...]:
        identities = [pair.identity for pair in pairs]
        if len(set(identities)) != len(identities):
            raise ValueError("candidate_pairs cannot contain duplicate pairs")
        return tuple(sorted(pairs, key=lambda pair: pair.identity))

    @model_validator(mode="after")
    def validate_candidate_scope(self) -> "ContradictionDetectionRequest":
        source_ids = {fact.fact_id for fact in self.source_facts}
        if any(
            pair.fact_a_id not in source_ids or pair.fact_b_id not in source_ids
            for pair in self.candidate_pairs
        ):
            raise ValueError("candidate pairs must reference source Facts")
        return self


def canonical_contradiction_detection_input_bytes(
    request: ContradictionDetectionRequest,
) -> bytes:
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def contradiction_detection_input_fingerprint(
    request: ContradictionDetectionRequest,
) -> str:
    digest = hashlib.sha256(canonical_contradiction_detection_input_bytes(request)).hexdigest()
    return f"sha256:{digest}"
