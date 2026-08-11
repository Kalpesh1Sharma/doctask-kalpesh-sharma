from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator


class RunType(StrEnum):
    INITIAL = "INITIAL"
    UPDATE = "UPDATE"
    MANUAL = "MANUAL"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RunStepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_UPSTREAM = "AWAITING_UPSTREAM"
    COMPLETED = "COMPLETED"
    RETRYING = "RETRYING"
    SKIPPED = "SKIPPED"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class Run(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    pile_id: str
    run_type: RunType
    status: RunStatus = RunStatus.PENDING

    trigger_document_version_id: str | None = None
    parent_run_id: str | None = None
    baseline_run_id: str | None = None
    blocked_on_run_id: str | None = None

    @model_validator(mode="after")
    def validate_baseline_and_blocker(self) -> "Run":
        if self.baseline_run_id is not None and self.blocked_on_run_id is not None:
            raise ValueError(
                "baseline_run_id and blocked_on_run_id cannot both be set",
            )

        return self


VALID_RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {
        RunStatus.RUNNING,
        RunStatus.FAILED,
    },
    RunStatus.RUNNING: {
        RunStatus.REVIEW_REQUIRED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.REVIEW_REQUIRED: {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
}


VALID_RUN_STEP_TRANSITIONS: dict[RunStepStatus, set[RunStepStatus]] = {
    RunStepStatus.PENDING: {
        RunStepStatus.RUNNING,
    },
    RunStepStatus.RUNNING: {
        RunStepStatus.COMPLETED,
        RunStepStatus.RETRYING,
        RunStepStatus.SKIPPED,
        RunStepStatus.ESCALATED,
        RunStepStatus.FAILED,
        RunStepStatus.AWAITING_UPSTREAM,
    },
    RunStepStatus.AWAITING_UPSTREAM: {
        RunStepStatus.RUNNING,
    },
    RunStepStatus.RETRYING: {
        RunStepStatus.RUNNING,
    },
    RunStepStatus.COMPLETED: set(),
    RunStepStatus.SKIPPED: set(),
    RunStepStatus.ESCALATED: set(),
    RunStepStatus.FAILED: set(),
}


def validate_status_transition(
    current: RunStatus,
    requested: RunStatus,
) -> bool:
    """Return whether a run status transition is valid."""
    if current == requested:
        return True

    return requested in VALID_RUN_TRANSITIONS[current]


def validate_run_step_transition(
    current: RunStepStatus,
    requested: RunStepStatus,
) -> bool:
    """Return whether a run-step status transition is valid."""
    if current == requested:
        return True

    return requested in VALID_RUN_STEP_TRANSITIONS[current]


def validate_trigger_document_version(
    run_type: RunType,
    trigger_document_version_id: str | None,
) -> bool:
    """Return whether the trigger document version matches the run type."""
    if run_type == RunType.UPDATE:
        return trigger_document_version_id is not None

    return trigger_document_version_id is None


def is_terminal_status(status: RunStatus) -> bool:
    """Return whether a run is in a terminal state."""
    return status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    }
