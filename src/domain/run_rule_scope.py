from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


def _canonical_uuid(value: str) -> str:
    return str(UUID(value))


class RunRuleSetSnapshot(BaseModel):
    """The copied RuleSet identity captured for one Run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    source_rule_set_id: str
    name: str
    description: str | None = None
    version: int = Field(gt=0)

    @field_validator("run_id", "source_rule_set_id")
    @classmethod
    def normalize_uuid(cls, value: str) -> str:
        return _canonical_uuid(value)


class RunRuleSnapshot(BaseModel):
    """The copied immutable Rule content captured for one Run."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    source_rule_id: str
    source_rule_set_id: str
    rule_key: str
    title: str
    description: str | None = None
    severity: str
    definition: JsonValue

    @field_validator("run_id", "source_rule_id", "source_rule_set_id")
    @classmethod
    def normalize_uuid(cls, value: str) -> str:
        return _canonical_uuid(value)
