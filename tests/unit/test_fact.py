import pytest

from domain.fact import Fact, FactStatus


def test_fact_defaults_to_confirmed() -> None:
    fact = Fact(
        fact_id="fact-1",
        origin_run_id="run-1",
        subject="Contract A",
        predicate="has_value",
        object_value="100000",
    )

    assert fact.status == FactStatus.CONFIRMED


@pytest.mark.parametrize(
    "status",
    [
        FactStatus.CONFIRMED,
        FactStatus.DISPUTED,
        FactStatus.SUPERSEDED,
    ],
)
def test_fact_supports_all_statuses(status: FactStatus) -> None:
    fact = Fact(
        fact_id="fact-1",
        origin_run_id="run-1",
        subject="Contract A",
        predicate="has_value",
        object_value="100000",
        status=status,
    )

    assert fact.status == status


@pytest.mark.parametrize(
    "field",
    [
        "fact_id",
        "origin_run_id",
        "subject",
        "predicate",
        "object_value",
        "status",
    ],
)
def test_fact_is_immutable(field: str) -> None:
    fact = Fact(
        fact_id="fact-1",
        origin_run_id="run-1",
        subject="Contract A",
        predicate="has_value",
        object_value="100000",
    )

    with pytest.raises((TypeError, ValueError)):
        setattr(
            fact,
            field,
            "changed",
        )