from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class FindingStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    origin_run_id: str
    title: str
    description: str | None = None
    status: FindingStatus = FindingStatus.PENDING


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