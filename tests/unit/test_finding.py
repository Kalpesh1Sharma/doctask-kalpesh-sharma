import pytest

from domain.finding import (
    FindingStatus,
    is_idempotent_decision,
    validate_status_transition,
)


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (FindingStatus.PENDING, FindingStatus.APPROVED),
        (FindingStatus.PENDING, FindingStatus.REJECTED),
        (FindingStatus.APPROVED, FindingStatus.APPROVED),
        (FindingStatus.REJECTED, FindingStatus.REJECTED),
    ],
)
def test_valid_finding_transitions(
    current: FindingStatus,
    requested: FindingStatus,
) -> None:
    assert validate_status_transition(current, requested) is True


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (FindingStatus.APPROVED, FindingStatus.REJECTED),
        (FindingStatus.REJECTED, FindingStatus.APPROVED),
    ],
)
def test_conflicting_terminal_decisions_are_invalid(
    current: FindingStatus,
    requested: FindingStatus,
) -> None:
    assert validate_status_transition(current, requested) is False


def test_identical_approved_decision_is_idempotent() -> None:
    assert (
        is_idempotent_decision(
            FindingStatus.APPROVED,
            FindingStatus.APPROVED,
        )
        is True
    )


def test_identical_rejected_decision_is_idempotent() -> None:
    assert (
        is_idempotent_decision(
            FindingStatus.REJECTED,
            FindingStatus.REJECTED,
        )
        is True
    )


def test_pending_decision_is_not_idempotent() -> None:
    assert (
        is_idempotent_decision(
            FindingStatus.PENDING,
            FindingStatus.APPROVED,
        )
        is False
    )


def test_finding_is_immutable() -> None:
    from domain.finding import Finding

    finding = Finding(
        finding_id="finding-1",
        origin_run_id="run-1",
        title="Test finding",
    )

    with pytest.raises((TypeError, ValueError)):
        finding.status = FindingStatus.APPROVED