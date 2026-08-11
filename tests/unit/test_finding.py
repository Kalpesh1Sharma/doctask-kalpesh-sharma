import pytest
from pydantic import ValidationError

from domain.finding import (
    Finding,
    FindingStatus,
    FindingType,
    is_idempotent_decision,
    validate_status_transition,
)


def make_finding(**overrides: object) -> Finding:
    values: dict[str, object] = {
        "finding_id": "finding-1",
        "origin_run_id": "run-1",
        "finding_type": FindingType.RULE_VIOLATION,
        "rule_id": "rule-1",
        "title": "Test finding",
        "severity": "HIGH",
    }
    values.update(overrides)
    return Finding(**values)


def test_finding_supports_rule_violation() -> None:
    finding = make_finding()

    assert finding.finding_type is FindingType.RULE_VIOLATION
    assert finding.rule_id == "rule-1"


def test_finding_supports_contradiction() -> None:
    finding = make_finding(
        finding_type=FindingType.CONTRADICTION,
        rule_id=None,
    )

    assert finding.finding_type is FindingType.CONTRADICTION
    assert finding.rule_id is None


def test_rule_violation_requires_rule_id() -> None:
    with pytest.raises(ValidationError, match="require rule_id"):
        make_finding(rule_id=None)


def test_contradiction_forbids_rule_id() -> None:
    with pytest.raises(ValidationError, match="forbid rule_id"):
        make_finding(
            finding_type=FindingType.CONTRADICTION,
            rule_id="rule-1",
        )


def test_finding_retains_severity() -> None:
    finding = make_finding(severity="CRITICAL")

    assert finding.severity == "CRITICAL"


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
    assert is_idempotent_decision(FindingStatus.APPROVED, FindingStatus.APPROVED) is True


def test_identical_rejected_decision_is_idempotent() -> None:
    assert is_idempotent_decision(FindingStatus.REJECTED, FindingStatus.REJECTED) is True


def test_pending_decision_is_not_idempotent() -> None:
    assert is_idempotent_decision(FindingStatus.PENDING, FindingStatus.APPROVED) is False


def test_finding_is_immutable() -> None:
    finding = make_finding()

    with pytest.raises(ValidationError):
        finding.status = FindingStatus.APPROVED
