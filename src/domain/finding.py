from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class FindingStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FindingType(StrEnum):
    RULE_VIOLATION = "RULE_VIOLATION"
    CONTRADICTION = "CONTRADICTION"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    origin_run_id: str
    finding_type: FindingType
    rule_id: str | None = None
    title: str
    description: str | None = None
    severity: str
    status: FindingStatus = FindingStatus.PENDING

    @model_validator(mode="after")
    def validate_rule_relationship(self) -> "Finding":
        if self.finding_type == FindingType.RULE_VIOLATION and self.rule_id is None:
            raise ValueError("RULE_VIOLATION findings require rule_id")

        if self.finding_type == FindingType.CONTRADICTION and self.rule_id is not None:
            raise ValueError("CONTRADICTION findings forbid rule_id")

        return self


def validate_status_transition(
    current: FindingStatus,
    requested: FindingStatus,
) -> bool:
    """Return whether a finding status change is valid."""

    if current == FindingStatus.PENDING:
        return requested in {
            FindingStatus.APPROVED,
            FindingStatus.REJECTED,
        }

    if current == FindingStatus.APPROVED:
        return requested == FindingStatus.APPROVED

    if current == FindingStatus.REJECTED:
        return requested == FindingStatus.REJECTED

    return False


def is_idempotent_decision(
    current: FindingStatus,
    requested: FindingStatus,
) -> bool:
    """Return whether the requested decision is an identical terminal no-op."""

    return (
        current in {
            FindingStatus.APPROVED,
            FindingStatus.REJECTED,
        }
        and current == requested
    )
