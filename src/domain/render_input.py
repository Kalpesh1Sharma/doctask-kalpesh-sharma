from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from math import isfinite
from types import MappingProxyType
from typing import Literal, Protocol, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from domain.fact import FactStatus
from domain.finding import FindingType
from domain.review_state import ReviewStateSnapshot, canonical_review_state_bytes
from domain.run_membership import RunMembershipRelation

_RENDER_INPUT_FORMAT = "render-input-v1"


def _canonical_uuid(value: str) -> str:
    return str(UUID(value))


def _freeze_json(value: object) -> object:
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("JSON values must not contain non-finite numbers")
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


def _normalize_id_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(_canonical_uuid(value) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError("identifier values must be unique")
    return tuple(sorted(normalized))


class _RunRuleScopeIdentity(Protocol):
    run_id: str


class RenderInputDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_version_id: str
    document_id: str
    version_number: int = Field(gt=0)
    content_hash: str

    @field_validator("document_version_id", "document_id")
    @classmethod
    def normalize_ids(cls, value: str) -> str:
        return _canonical_uuid(value)


class RenderInputFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_id: str
    origin_run_id: str
    relation: RunMembershipRelation
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus

    @field_validator("fact_id", "origin_run_id")
    @classmethod
    def normalize_ids(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("object_value")
    @classmethod
    def freeze_object_value(cls, value: JsonValue) -> object:
        return _freeze_json(value)

    @field_serializer("object_value")
    def serialize_object_value(self, value: object) -> JsonValue:
        return _thaw_json(value)


class RenderInputEvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

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
    def normalize_ids(cls, value: str) -> str:
        return _canonical_uuid(value)


class FactEvidenceReferences(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_id: str
    evidence_span_ids: tuple[str, ...]

    @field_validator("fact_id")
    @classmethod
    def normalize_fact_id(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("evidence_span_ids")
    @classmethod
    def normalize_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_id_tuple(values)


class FindingEvidenceReferences(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    evidence_span_ids: tuple[str, ...]

    @field_validator("finding_id")
    @classmethod
    def normalize_finding_id(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("evidence_span_ids")
    @classmethod
    def normalize_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _normalize_id_tuple(values)


class RenderInputRuleSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_rule_set_id: str
    name: str
    description: str | None = None
    version: int = Field(gt=0)

    @field_validator("source_rule_set_id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        return _canonical_uuid(value)


class RenderInputRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_rule_id: str
    source_rule_set_id: str
    rule_key: str
    title: str
    description: str | None = None
    severity: str
    definition: JsonValue

    @field_validator("source_rule_id", "source_rule_set_id")
    @classmethod
    def normalize_ids(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("definition")
    @classmethod
    def freeze_definition(cls, value: JsonValue) -> object:
        return _freeze_json(value)

    @field_serializer("definition")
    def serialize_definition(self, value: object) -> JsonValue:
        return _thaw_json(value)


class FindingOriginRuleSet(RenderInputRuleSet):
    origin_run_id: str

    @field_validator("origin_run_id")
    @classmethod
    def normalize_origin_run_id(cls, value: str) -> str:
        return _canonical_uuid(value)


class FindingOriginRule(RenderInputRule):
    origin_run_id: str

    @field_validator("origin_run_id")
    @classmethod
    def normalize_origin_run_id(cls, value: str) -> str:
        return _canonical_uuid(value)


class RenderInputSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    documents: tuple[RenderInputDocument, ...] = ()
    facts: tuple[RenderInputFact, ...] = ()
    evidence_spans: tuple[RenderInputEvidenceSpan, ...] = ()
    fact_evidence: tuple[FactEvidenceReferences, ...] = ()
    finding_evidence: tuple[FindingEvidenceReferences, ...] = ()
    evaluated_rule_sets: tuple[RenderInputRuleSet, ...] = ()
    evaluated_rules: tuple[RenderInputRule, ...] = ()
    finding_origin_rule_sets: tuple[FindingOriginRuleSet, ...] = ()
    finding_origin_rules: tuple[FindingOriginRule, ...] = ()

    @model_validator(mode="after")
    def sort_and_validate_collections(self) -> RenderInputSource:
        collections: tuple[tuple[str, object], ...] = (
            ("documents", self.documents),
            ("facts", self.facts),
            ("evidence_spans", self.evidence_spans),
            ("fact_evidence", self.fact_evidence),
            ("finding_evidence", self.finding_evidence),
            ("evaluated_rule_sets", self.evaluated_rule_sets),
            ("evaluated_rules", self.evaluated_rules),
            ("finding_origin_rule_sets", self.finding_origin_rule_sets),
            ("finding_origin_rules", self.finding_origin_rules),
        )
        sort_keys = {
            "documents": lambda item: item.document_version_id,
            "facts": lambda item: item.fact_id,
            "evidence_spans": lambda item: item.evidence_id,
            "fact_evidence": lambda item: item.fact_id,
            "finding_evidence": lambda item: item.finding_id,
            "evaluated_rule_sets": lambda item: item.source_rule_set_id,
            "evaluated_rules": lambda item: item.source_rule_id,
            "finding_origin_rule_sets": lambda item: (item.origin_run_id, item.source_rule_set_id),
            "finding_origin_rules": lambda item: (item.origin_run_id, item.source_rule_id),
        }
        for name, values in collections:
            sorted_values = tuple(sorted(cast(tuple[object, ...], values), key=sort_keys[name]))
            ids = [sort_keys[name](item) for item in sorted_values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} must have unique identities")
            object.__setattr__(self, name, sorted_values)
        return self


class RenderInputManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    format: Literal["render-input-v1"] = _RENDER_INPUT_FORMAT
    run_id: str
    review_state: ReviewStateSnapshot
    source: RenderInputSource

    @field_validator("run_id")
    @classmethod
    def normalize_run_id(cls, value: str) -> str:
        return _canonical_uuid(value)


def build_render_input_manifest(
    run_id: str,
    review_state: ReviewStateSnapshot,
    source: RenderInputSource,
    rule_scope: _RunRuleScopeIdentity | None,
) -> RenderInputManifest:
    normalized_run_id = _canonical_uuid(run_id)
    if rule_scope is None:
        raise ValueError("a completed run rule scope is required")
    if review_state.run_id != normalized_run_id or _canonical_uuid(rule_scope.run_id) != normalized_run_id:
        raise ValueError("manifest, review state, and rule scope must identify the same run")

    fact_ids = {fact.fact_id for fact in source.facts}
    finding_ids = {membership.finding_id for membership in review_state.memberships}
    evidence_ids = {evidence.evidence_id for evidence in source.evidence_spans}
    document_version_ids = {document.document_version_id for document in source.documents}
    fact_reference_ids = {reference.fact_id for reference in source.fact_evidence}
    finding_reference_ids = {reference.finding_id for reference in source.finding_evidence}

    if fact_reference_ids != fact_ids:
        raise ValueError("fact evidence references must match included facts")
    if finding_reference_ids != finding_ids:
        raise ValueError("finding evidence references must match review-state findings")

    referenced_evidence_ids = {
        evidence_id
        for reference in (*source.fact_evidence, *source.finding_evidence)
        for evidence_id in reference.evidence_span_ids
    }
    if referenced_evidence_ids != evidence_ids:
        raise ValueError("evidence spans must exactly match evidence references")
    if any(evidence.document_version_id not in document_version_ids for evidence in source.evidence_spans):
        raise ValueError("evidence spans must belong to this run's document inputs")

    evaluated_rule_set_ids = {rule_set.source_rule_set_id for rule_set in source.evaluated_rule_sets}
    if any(rule.source_rule_set_id not in evaluated_rule_set_ids for rule in source.evaluated_rules):
        raise ValueError("evaluated rules must reference an evaluated rule set")

    evaluated_rule_ids = {rule.source_rule_id for rule in source.evaluated_rules}
    required_origin_keys = {
        (membership.origin_run_id, membership.rule_id)
        for membership in review_state.memberships
        if membership.finding_type is FindingType.RULE_VIOLATION
        and membership.origin_run_id != normalized_run_id
        and membership.rule_id is not None
    }
    for membership in review_state.memberships:
        if (
            membership.finding_type is FindingType.RULE_VIOLATION
            and membership.origin_run_id == normalized_run_id
            and membership.rule_id not in evaluated_rule_ids
        ):
            raise ValueError("current-run rule violation requires an evaluated rule snapshot")

    origin_rules = {(rule.origin_run_id, rule.source_rule_id): rule for rule in source.finding_origin_rules}
    if set(origin_rules) != required_origin_keys:
        raise ValueError("carried rule violations require exactly matching origin rule snapshots")
    origin_rule_sets = {
        (rule_set.origin_run_id, rule_set.source_rule_set_id)
        for rule_set in source.finding_origin_rule_sets
    }
    required_origin_rule_sets = {
        (rule.origin_run_id, rule.source_rule_set_id)
        for rule in origin_rules.values()
    }
    if origin_rule_sets != required_origin_rule_sets:
        raise ValueError("origin rule snapshots require exactly matching origin rule-set snapshots")

    return RenderInputManifest(
        run_id=normalized_run_id,
        review_state=review_state,
        source=source,
    )


def _canonical_source_value(source: RenderInputSource) -> dict[str, object]:
    return {
        "documents": [document.model_dump(mode="json") for document in source.documents],
        "facts": [fact.model_dump(mode="json") for fact in source.facts],
        "evidence_spans": [evidence.model_dump(mode="json") for evidence in source.evidence_spans],
        "fact_evidence": [reference.model_dump(mode="json") for reference in source.fact_evidence],
        "finding_evidence": [reference.model_dump(mode="json") for reference in source.finding_evidence],
        "evaluated_rule_sets": [rule_set.model_dump(mode="json") for rule_set in source.evaluated_rule_sets],
        "evaluated_rules": [rule.model_dump(mode="json") for rule in source.evaluated_rules],
        "finding_origin_rule_sets": [
            rule_set.model_dump(mode="json") for rule_set in source.finding_origin_rule_sets
        ],
        "finding_origin_rules": [rule.model_dump(mode="json") for rule in source.finding_origin_rules],
    }


def render_input_canonical_bytes(manifest: RenderInputManifest) -> bytes:
    value = {
        "format": manifest.format,
        "run_id": manifest.run_id,
        "review_state": json.loads(canonical_review_state_bytes(manifest.review_state)),
        "source": _canonical_source_value(manifest.source),
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def render_input_fingerprint(manifest: RenderInputManifest) -> str:
    return f"sha256:{sha256(render_input_canonical_bytes(manifest)).hexdigest()}"
