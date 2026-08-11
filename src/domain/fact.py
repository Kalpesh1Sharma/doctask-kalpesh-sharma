from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
    object_value: str
    status: FactStatus = FactStatus.CONFIRMED