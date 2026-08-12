import pytest

from domain.run_membership import RunMembershipRelation


@pytest.mark.parametrize(
    ("relation", "value"),
    [
        (RunMembershipRelation.CREATED, "CREATED"),
        (RunMembershipRelation.CARRIED_FORWARD, "CARRIED_FORWARD"),
    ],
)
def test_run_membership_relations_match_locked_vocabulary(
    relation: RunMembershipRelation,
    value: str,
) -> None:
    assert relation.value == value
