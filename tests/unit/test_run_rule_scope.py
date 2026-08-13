from uuid import uuid4

import pytest
from pydantic import ValidationError

from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot


def test_rule_set_snapshot_normalizes_ids_and_rejects_non_positive_version() -> None:
    snapshot = RunRuleSetSnapshot(
        run_id="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        source_rule_set_id="BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
        name="Contract rules",
        description=None,
        version=1,
    )

    assert snapshot.run_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert snapshot.source_rule_set_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    with pytest.raises(ValidationError):
        RunRuleSetSnapshot(
            run_id=str(uuid4()),
            source_rule_set_id=str(uuid4()),
            name="Contract rules",
            version=0,
        )


def test_rule_snapshot_is_frozen_and_preserves_json_definition() -> None:
    snapshot = RunRuleSnapshot(
        run_id=str(uuid4()),
        source_rule_id=str(uuid4()),
        source_rule_set_id=str(uuid4()),
        rule_key="required-clause",
        title="Required clause",
        description="A nested definition is preserved.",
        severity="HIGH",
        definition={"all": [True, {"field": "governing_law"}]},
    )

    assert snapshot.model_dump(mode="json")["definition"] == {
        "all": [True, {"field": "governing_law"}],
    }

    with pytest.raises(ValidationError):
        snapshot.title = "rewritten"
