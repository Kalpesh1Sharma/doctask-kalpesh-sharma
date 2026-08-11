from enum import StrEnum

from pydantic import BaseModel, ConfigDict, JsonValue


class FactStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DISPUTED = "DISPUTED"
    SUPERSEDED = "SUPERSEDED"


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True)

    fact_id: str
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus = FactStatus.CONFIRMED
