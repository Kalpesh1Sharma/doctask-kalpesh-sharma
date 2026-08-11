import pytest

from domain.run import (
    RunStatus,
    RunType,
    is_terminal_status,
    validate_status_transition,
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


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (RunStatus.RUNNING, RunStatus.AWAITING_UPSTREAM),
        (RunStatus.RUNNING, RunStatus.RETRYING),
        (RunStatus.RUNNING, RunStatus.REVIEW_REQUIRED),
        (RunStatus.RUNNING, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.FAILED),
        (RunStatus.AWAITING_UPSTREAM, RunStatus.RUNNING),
        (RunStatus.AWAITING_UPSTREAM, RunStatus.FAILED),
        (RunStatus.RETRYING, RunStatus.RUNNING),
        (RunStatus.RETRYING, RunStatus.FAILED),
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
        (RunStatus.AWAITING_UPSTREAM, RunStatus.COMPLETED),
        (RunStatus.RETRYING, RunStatus.COMPLETED),
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
        RunStatus.RUNNING,
        RunStatus.AWAITING_UPSTREAM,
        RunStatus.RETRYING,
        RunStatus.REVIEW_REQUIRED,
    ],
)
def test_non_terminal_statuses(status: RunStatus) -> None:
    assert is_terminal_status(status) is False