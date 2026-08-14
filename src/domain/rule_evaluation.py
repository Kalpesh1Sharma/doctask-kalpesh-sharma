"""Pure contracts for immutable Run-rule evaluation."""

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
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


def _contains_non_finite(value: object) -> bool:
    if isinstance(value, float):
        return not isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_non_finite(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
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
    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class RuleEvaluationSourceEvidence(BaseModel):
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


class RuleEvaluationSourceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    origin_run_id: str
    relation: RunMembershipRelation
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus
    evidence: tuple[RuleEvaluationSourceEvidence, ...]

    @field_validator("fact_id", "origin_run_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @field_validator("object_value")
    @classmethod
    def validate_and_freeze_json(cls, value: JsonValue) -> object:
        if _contains_non_finite(value):
            raise ValueError("object_value must not contain non-finite numbers")
        return _freeze_json(value)

    @field_serializer("object_value")
    def serialize_object_value(self, value: object) -> JsonValue:
        return _thaw_json(value)

    @field_validator("evidence")
    @classmethod
    def normalize_evidence(
        cls,
        evidence: tuple[RuleEvaluationSourceEvidence, ...],
    ) -> tuple[RuleEvaluationSourceEvidence, ...]:
        if len({item.evidence_id for item in evidence}) != len(evidence):
            raise ValueError("evidence cannot contain duplicate IDs")
        return tuple(sorted(evidence, key=lambda item: item.evidence_id))


class RuleEvaluationRuleSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_rule_set_id: str
    name: str
    description: str | None = None
    version: int

    @field_validator("source_rule_set_id")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        return normalize_uuid(value, "source_rule_set_id")


class RuleEvaluationRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_rule_id: str
    source_rule_set_id: str
    rule_key: str
    title: str
    description: str | None = None
    severity: str
    definition: JsonValue

    @field_validator("source_rule_id", "source_rule_set_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @field_validator("definition")
    @classmethod
    def validate_and_freeze_definition(cls, value: JsonValue) -> object:
        if _contains_non_finite(value):
            raise ValueError("definition must not contain non-finite numbers")
        return _freeze_json(value)

    @field_serializer("definition")
    def serialize_definition(self, value: object) -> JsonValue:
        return _thaw_json(value)


class RuleOutcomeType(StrEnum):
    NO_VIOLATION = "NO_VIOLATION"
    VIOLATIONS = "VIOLATIONS"
    UNSUPPORTED = "UNSUPPORTED"


class RuleViolationCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    supporting_fact_ids: tuple[str, ...]

    @field_validator("supporting_fact_ids")
    @classmethod
    def normalize_fact_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_uuid(value, "supporting_fact_ids") for value in values)
        if not normalized:
            raise ValueError("a violation requires supporting Facts")
        return tuple(sorted(set(normalized)))

    @property
    def identity(self) -> tuple[str, ...]:
        return self.supporting_fact_ids


class RuleEvaluationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_rule_id: str
    outcome: RuleOutcomeType
    violations: tuple[RuleViolationCandidate, ...] = ()

    @field_validator("source_rule_id")
    @classmethod
    def normalize_rule_id(cls, value: str) -> str:
        return normalize_uuid(value, "source_rule_id")

    @field_validator("violations")
    @classmethod
    def normalize_violations(
        cls,
        violations: tuple[RuleViolationCandidate, ...],
    ) -> tuple[RuleViolationCandidate, ...]:
        unique = {candidate.identity: candidate for candidate in violations}
        return tuple(unique[key] for key in sorted(unique))

    @model_validator(mode="after")
    def validate_outcome(self) -> "RuleEvaluationOutcome":
        if self.outcome is RuleOutcomeType.VIOLATIONS and not self.violations:
            raise ValueError("VIOLATIONS requires at least one candidate")
        if self.outcome is not RuleOutcomeType.VIOLATIONS and self.violations:
            raise ValueError("only VIOLATIONS may include candidates")
        return self


class RuleEvaluationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["rule-evaluation-v1"]
    outcomes: tuple[RuleEvaluationOutcome, ...]

    @field_validator("outcomes")
    @classmethod
    def normalize_outcomes(
        cls,
        outcomes: tuple[RuleEvaluationOutcome, ...],
    ) -> tuple[RuleEvaluationOutcome, ...]:
        if len({outcome.source_rule_id for outcome in outcomes}) != len(outcomes):
            raise ValueError("outcomes cannot contain duplicate Rule IDs")
        return tuple(sorted(outcomes, key=lambda outcome: outcome.source_rule_id))


class RuleEvaluationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["rule-evaluation-v1"] = "rule-evaluation-v1"
    run_id: str
    instruction_version: str
    trusted_instructions: str
    rule_sets: tuple[RuleEvaluationRuleSet, ...]
    rules: tuple[RuleEvaluationRule, ...]
    source_facts: tuple[RuleEvaluationSourceFact, ...]

    @field_validator("run_id")
    @classmethod
    def normalize_run_id(cls, value: str) -> str:
        return normalize_uuid(value, "run_id")

    @field_validator("rule_sets")
    @classmethod
    def normalize_rule_sets(
        cls,
        rule_sets: tuple[RuleEvaluationRuleSet, ...],
    ) -> tuple[RuleEvaluationRuleSet, ...]:
        if len({item.source_rule_set_id for item in rule_sets}) != len(rule_sets):
            raise ValueError("rule_sets cannot contain duplicate IDs")
        return tuple(sorted(rule_sets, key=lambda item: item.source_rule_set_id))

    @field_validator("rules")
    @classmethod
    def normalize_rules(cls, rules: tuple[RuleEvaluationRule, ...]) -> tuple[RuleEvaluationRule, ...]:
        if len({item.source_rule_id for item in rules}) != len(rules):
            raise ValueError("rules cannot contain duplicate IDs")
        return tuple(sorted(rules, key=lambda item: item.source_rule_id))

    @field_validator("source_facts")
    @classmethod
    def normalize_facts(
        cls,
        facts: tuple[RuleEvaluationSourceFact, ...],
    ) -> tuple[RuleEvaluationSourceFact, ...]:
        if len({item.fact_id for item in facts}) != len(facts):
            raise ValueError("source_facts cannot contain duplicate IDs")
        return tuple(sorted(facts, key=lambda item: item.fact_id))

    @model_validator(mode="after")
    def validate_rule_set_scope(self) -> "RuleEvaluationRequest":
        rule_set_ids = {item.source_rule_set_id for item in self.rule_sets}
        if any(rule.source_rule_set_id not in rule_set_ids for rule in self.rules):
            raise ValueError("rules must belong to supplied rule_sets")
        return self


def canonical_rule_evaluation_input_bytes(request: RuleEvaluationRequest) -> bytes:
    return json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def rule_evaluation_input_fingerprint(request: RuleEvaluationRequest) -> str:
    return f"sha256:{hashlib.sha256(canonical_rule_evaluation_input_bytes(request)).hexdigest()}"
