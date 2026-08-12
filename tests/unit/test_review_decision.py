import pytest

from domain.review_decision import ReviewDecisionType


@pytest.mark.parametrize(
    ("decision", "value"),
    [
        (ReviewDecisionType.APPROVED, "APPROVED"),
        (ReviewDecisionType.REJECTED, "REJECTED"),
    ],
)
def test_review_decision_type_uses_the_locked_vocabulary(
    decision: ReviewDecisionType,
    value: str,
) -> None:
    assert decision.value == value
