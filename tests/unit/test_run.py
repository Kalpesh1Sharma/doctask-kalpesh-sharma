import pytest
from pydantic import ValidationError

from domain.run import (
    Run,
    RunStatus,
    RunStepStatus,
    RunType,
    is_terminal_status,
    validate_run_step_transition,
    validate_status_transition,
    validate_trigger_document_version,
)


@pytest.mark.parametrize(
    "run_type",
    [
        RunType.INITIAL,
        RunType.UPDATE,
        RunType.MANUAL,
    ],
)
def test_supported_run_types(run_type: RunType) -> None:
    assert run_type in {
        RunType.INITIAL,
        RunType.UPDATE,
        RunType.MANUAL,
    }


def test_run_defaults_to_pending() -> None:
    run = Run(
        run_id="run-1",
        pile_id="pile-1",
        run_type=RunType.INITIAL,
    )

    assert run.status is RunStatus.PENDING


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (RunStatus.PENDING, RunStatus.RUNNING),
        (RunStatus.PENDING, RunStatus.FAILED),
        (RunStatus.RUNNING, RunStatus.REVIEW_REQUIRED),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.REVIEW_REQUIRED, RunStatus.COMPLETED),
        (RunStatus.REVIEW_REQUIRED, RunStatus.FAILED),
    ],
)
def test_valid_run_transitions(
    current: RunStatus,
    requested: RunStatus,
) -> None:
    assert validate_status_transition(current, requested) is True


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (RunStatus.COMPLETED, RunStatus.RUNNING),
        (RunStatus.COMPLETED, RunStatus.FAILED),
        (RunStatus.FAILED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.COMPLETED),
        (RunStatus.REVIEW_REQUIRED, RunStatus.RUNNING),
        (RunStatus.PENDING, RunStatus.COMPLETED),
    ],
)
def test_invalid_run_transitions(
    current: RunStatus,
    requested: RunStatus,
) -> None:
    assert validate_status_transition(current, requested) is False


@pytest.mark.parametrize("status", list(RunStatus))
def test_same_status_is_idempotent(status: RunStatus) -> None:
    assert validate_status_transition(status, status) is True


def test_terminal_statuses() -> None:
    assert is_terminal_status(RunStatus.COMPLETED) is True
    assert is_terminal_status(RunStatus.FAILED) is True


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.PENDING,
        RunStatus.RUNNING,
        RunStatus.REVIEW_REQUIRED,
    ],
)
def test_non_terminal_statuses(status: RunStatus) -> None:
    assert is_terminal_status(status) is False


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (RunStepStatus.PENDING, RunStepStatus.RUNNING),
        (RunStepStatus.RUNNING, RunStepStatus.COMPLETED),
        (RunStepStatus.RUNNING, RunStepStatus.RETRYING),
        (RunStepStatus.RUNNING, RunStepStatus.SKIPPED),
        (RunStepStatus.RUNNING, RunStepStatus.ESCALATED),
        (RunStepStatus.RUNNING, RunStepStatus.FAILED),
        (RunStepStatus.RUNNING, RunStepStatus.AWAITING_UPSTREAM),
        (RunStepStatus.AWAITING_UPSTREAM, RunStepStatus.RUNNING),
        (RunStepStatus.RETRYING, RunStepStatus.RUNNING),
    ],
)
def test_valid_run_step_transitions(
    current: RunStepStatus,
    requested: RunStepStatus,
) -> None:
    assert validate_run_step_transition(current, requested) is True


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (RunStepStatus.PENDING, RunStepStatus.COMPLETED),
        (RunStepStatus.PENDING, RunStepStatus.SKIPPED),
        (RunStepStatus.AWAITING_UPSTREAM, RunStepStatus.COMPLETED),
        (RunStepStatus.RETRYING, RunStepStatus.FAILED),
        (RunStepStatus.COMPLETED, RunStepStatus.RUNNING),
        (RunStepStatus.SKIPPED, RunStepStatus.RUNNING),
        (RunStepStatus.ESCALATED, RunStepStatus.RUNNING),
        (RunStepStatus.FAILED, RunStepStatus.RUNNING),
    ],
)
def test_invalid_run_step_transitions(
    current: RunStepStatus,
    requested: RunStepStatus,
) -> None:
    assert validate_run_step_transition(current, requested) is False


@pytest.mark.parametrize("status", list(RunStepStatus))
def test_same_run_step_status_is_idempotent(status: RunStepStatus) -> None:
    assert validate_run_step_transition(status, status) is True


@pytest.mark.parametrize(
    ("run_type", "trigger_document_version_id"),
    [
        (RunType.UPDATE, "document-version-1"),
        (RunType.INITIAL, None),
        (RunType.MANUAL, None),
    ],
)
def test_valid_trigger_document_version(
    run_type: RunType,
    trigger_document_version_id: str | None,
) -> None:
    assert (
        validate_trigger_document_version(
            run_type,
            trigger_document_version_id,
        )
        is True
    )


@pytest.mark.parametrize(
    ("run_type", "trigger_document_version_id"),
    [
        (RunType.UPDATE, None),
        (RunType.INITIAL, "document-version-1"),
        (RunType.MANUAL, "document-version-1"),
    ],
)
def test_invalid_trigger_document_version(
    run_type: RunType,
    trigger_document_version_id: str | None,
) -> None:
    assert (
        validate_trigger_document_version(
            run_type,
            trigger_document_version_id,
        )
        is False
    )


def test_run_contains_trigger_document_version_id() -> None:
    run = Run(
        run_id="run-1",
        pile_id="pile-1",
        run_type=RunType.UPDATE,
        trigger_document_version_id="document-version-1",
    )

    assert run.trigger_document_version_id == "document-version-1"


def test_run_is_immutable() -> None:
    run = Run(
        run_id="run-1",
        pile_id="pile-1",
        run_type=RunType.INITIAL,
    )

    with pytest.raises(ValidationError):
        run.status = RunStatus.RUNNING