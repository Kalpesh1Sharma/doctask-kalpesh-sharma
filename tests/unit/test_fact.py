import json
from math import inf, nan

import pytest
from pydantic import ValidationError

from domain.fact import Fact, FactStatus


def make_fact(object_value: object = "100000", **overrides: object) -> Fact:
    values: dict[str, object] = {
        "fact_id": "fact-1",
        "origin_run_id": "run-1",
        "subject": "Contract A",
        "predicate": "has_value",
        "object_value": object_value,
    }
    values.update(overrides)
    return Fact(**values)


@pytest.mark.parametrize(
    "object_value",
    [
        "100000",
        100000,
        True,
        ["monthly", 1000, False],
        {"amount": 100000, "currency": "USD"},
        None,
    ],
)
def test_fact_supports_json_compatible_object_values(object_value: object) -> None:
    fact = make_fact(object_value)

    assert fact.model_dump(mode="json")["object_value"] == object_value
    assert json.loads(fact.model_dump_json())["object_value"] == object_value


def test_fact_object_value_is_deeply_immutable() -> None:
    fact = make_fact(
        {
            "nested": {
                "items": [
                    {"amount": 100000},
                ],
            },
        },
    )

    with pytest.raises(TypeError):
        fact.object_value["nested"] = "changed"

    with pytest.raises(AttributeError):
        fact.object_value["nested"]["items"].append("changed")

    with pytest.raises(TypeError):
        fact.object_value["nested"]["items"][0]["amount"] = 1


@pytest.mark.parametrize("object_value", [nan, inf, -inf])
def test_fact_rejects_non_standard_numeric_values(object_value: float) -> None:
    with pytest.raises(ValidationError, match="non-finite"):
        make_fact(object_value)


def test_fact_rejects_nested_non_standard_numeric_values() -> None:
    with pytest.raises(ValidationError, match="non-finite"):
        make_fact({"amounts": [100000, nan]})


def test_fact_defaults_to_confirmed() -> None:
    fact = make_fact()

    assert fact.status is FactStatus.CONFIRMED


@pytest.mark.parametrize(
    "status",
    [
        FactStatus.CONFIRMED,
        FactStatus.DISPUTED,
        FactStatus.SUPERSEDED,
    ],
)
def test_fact_supports_all_statuses(status: FactStatus) -> None:
    fact = make_fact(status=status)

    assert fact.status is status


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
    fact = make_fact()

    with pytest.raises(ValidationError):
        setattr(fact, field, "changed")
