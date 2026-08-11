from collections.abc import Mapping
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import cast

from pydantic import BaseModel, ConfigDict, JsonValue, field_serializer, field_validator


class FactStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"


def _contains_non_finite_number(value: object) -> bool:
    if isinstance(value, float):
        return not isfinite(value)

    if isinstance(value, Mapping):
        return any(_contains_non_finite_number(item) for item in value.values())

    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite_number(item) for item in value)

    return False


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json_value(item) for key, item in value.items()},
        )

    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)

    return value


def _thaw_json_value(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            key: _thaw_json_value(item)
            for key, item in value.items()
        }

    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]

    return cast(JsonValue, value)


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_id: str
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus = FactStatus.CONFIRMED

    @field_validator("object_value")
    @classmethod
    def validate_and_freeze_object_value(cls, value: JsonValue) -> object:
        if _contains_non_finite_number(value):
            raise ValueError("object_value must not contain non-finite numbers")

        return _freeze_json_value(value)

    @field_serializer("object_value")
    def serialize_object_value(self, value: object) -> JsonValue:
        return _thaw_json_value(value)
