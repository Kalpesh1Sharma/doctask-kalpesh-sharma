from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RunType(StrEnum):
    INITIAL = "INITIAL"
    UPDATE = "UPDATE"
    MANUAL = "MANUAL"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    AWAITING_UPSTREAM = "AWAITING_UPSTREAM"
    RETRYING = "RETRYING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Run(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    pile_id: str
    run_type: RunType
    status: RunStatus = RunStatus.RUNNING

    parent_run_id: str | None = None
    baseline_run_id: str | None = None
    blocked_on_run_id: str | None = None


VALID_RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.RUNNING: {
        RunStatus.AWAITING_UPSTREAM,
        RunStatus.RETRYING,
        RunStatus.REVIEW_REQUIRED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.AWAITING_UPSTREAM: {
        RunStatus.RUNNING,
        RunStatus.FAILED,
    },
    RunStatus.RETRYING: {
        RunStatus.RUNNING,
        RunStatus.FAILED,
    },
    RunStatus.REVIEW_REQUIRED: {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
}


def validate_status_transition(
    current: RunStatus,
    requested: RunStatus,
) -> bool:
    """Return whether a run status transition is valid."""

    if current == requested:
        return True

    return requested in VALID_RUN_TRANSITIONS[current]


def is_terminal_status(status: RunStatus) -> bool:
    """Return whether a run is in a terminal state."""

    return status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    }