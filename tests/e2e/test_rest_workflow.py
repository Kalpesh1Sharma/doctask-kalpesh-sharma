from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from .support import ExternalE2EHarness, external_e2e_harness


@pytest.fixture(scope="module")
def harness() -> Iterator[ExternalE2EHarness]:
    with external_e2e_harness("task1_phase6b_rest") as value:
        yield value


@pytest.fixture
def client(harness: ExternalE2EHarness) -> Iterator[TestClient]:
    with TestClient(harness.app) as value:
        yield value


def _create_pile_and_rules(client: TestClient) -> tuple[str, str, list[str]]:
    pile = client.post(
        "/api/v1/piles",
        json={
            "name": "REST black-box pile",
            "description": "Phase 6B-6",
            "watch_enabled": False,
            "watch_location": None,
        },
    )
    assert pile.status_code == 201, pile.text
    pile_id = pile.json()["pile_id"]

    rule_set = client.post(
        f"/api/v1/piles/{pile_id}/rule-sets",
        json={
            "name": "E2E playbook",
            "description": "Exercise mixed review decisions",
            "version": 1,
            "rules": [
                {
                    "rule_key": "payment-term",
                    "title": "Payment term maximum",
                    "description": "Flag payment terms over the preferred limit",
                    "severity": "HIGH",
                    "definition": {"max_days": 45},
                },
                {
                    "rule_key": "secondary-check",
                    "title": "Secondary reviewer check",
                    "description": "Second deterministic finding for mixed decisions",
                    "severity": "MEDIUM",
                    "definition": {"required": True},
                },
            ],
        },
    )
    assert rule_set.status_code == 201, rule_set.text
    payload = rule_set.json()
    return pile_id, payload["rule_set_id"], [rule["rule_id"] for rule in payload["rules"]]


def _ingest(client: TestClient, pile_id: str, logical_key: str, content: str) -> str:
    response = client.post(
        f"/api/v1/piles/{pile_id}/documents",
        data={"logical_key": logical_key},
        files={"file": (f"{logical_key}.txt", content.encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["replayed"] is False
    assert len(payload["created_run_ids"]) == 1
    return payload["created_run_ids"][0]


def _advance_to_review(
    client: TestClient,
    run_id: str,
    rule_set_id: str,
    rule_ids: list[str],
) -> None:
    request = {"source_rule_set_ids": [rule_set_id], "source_rule_ids": rule_ids}
    expected = ["INGEST", "UNDERSTAND", "EXAMINE", "REVIEW"]
    for stage in expected:
        response = client.post(f"/api/v1/runs/{run_id}/advance", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["stage"] == stage
    run = client.get(f"/api/v1/runs/{run_id}")
    assert run.status_code == 200
    assert run.json()["status"] == "REVIEW_REQUIRED"


def _review_findings_mixed(client: TestClient, run_id: str) -> dict:
    response = client.get(f"/api/v1/runs/{run_id}/review")
    assert response.status_code == 200
    review = response.json()
    assert len(review["findings"]) == 2
    assert review["finding_review_complete"] is False
    assert review["commit_ready"] is False
    assert all(item["evidence"] for item in review["findings"])
    assert all(
        "Payment term is" in item["evidence"][0]["source_text"]
        for item in review["findings"]
    )

    for index, finding in enumerate(review["findings"]):
        decision = "APPROVED" if index == 0 else "REJECTED"
        result = client.post(
            f"/api/v1/runs/{run_id}/findings/{finding['finding_id']}/decision",
            json={
                "decision": decision,
                "actor_type": "HUMAN",
                "actor_id": "rest-e2e@example.test",
                "idempotency_key": f"review:{run_id}:{finding['finding_id']}",
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["decision"] == decision

    completed = client.get(f"/api/v1/runs/{run_id}/review").json()
    assert completed["finding_review_complete"] is True
    return completed


def _commit_and_read_report(
    client: TestClient,
    run_id: str,
    rule_set_id: str,
    rule_ids: list[str],
) -> tuple[dict, str]:
    response = client.post(
        f"/api/v1/runs/{run_id}/advance",
        json={"source_rule_set_ids": [rule_set_id], "source_rule_ids": rule_ids},
    )
    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "COMMIT"
    assert response.json()["run_status"] == "COMPLETED"

    metadata = client.get(f"/api/v1/runs/{run_id}/report")
    content = client.get(f"/api/v1/runs/{run_id}/report/content")
    assert metadata.status_code == 200, metadata.text
    assert content.status_code == 200, content.text
    assert content.headers["x-content-sha256"] == metadata.json()["content_hash"]
    assert "# Final Report" in content.text
    return metadata.json(), content.text


def test_rest_initial_and_focused_update_are_fully_machine_drivable(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    pile_id, rule_set_id, rule_ids = _create_pile_and_rules(client)
    initial_run_id = _ingest(
        client,
        pile_id,
        "master-contract",
        "Master Agreement\nPayment term is 60 days.\nEnd.\n",
    )
    _advance_to_review(client, initial_run_id, rule_set_id, rule_ids)

    blocked = client.post(
        f"/api/v1/runs/{initial_run_id}/advance",
        json={"source_rule_set_ids": [rule_set_id], "source_rule_ids": rule_ids},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "HUMAN_REVIEW_REQUIRED"

    initial_review = _review_findings_mixed(client, initial_run_id)
    assert initial_review["update_change_set"] is None
    assert initial_review["review_complete"] is True
    assert initial_review["commit_ready"] is True
    initial_report, initial_content = _commit_and_read_report(
        client,
        initial_run_id,
        rule_set_id,
        rule_ids,
    )
    assert initial_report["focused_update"] is None

    metrics = client.get(f"/api/v1/runs/{initial_run_id}/metrics")
    assert metrics.status_code == 200
    assert {item["stage"] for item in metrics.json()} == {
        "INGEST",
        "UNDERSTAND",
        "EXAMINE",
        "REVIEW",
        "COMMIT",
    }

    update_run_id = _ingest(
        client,
        pile_id,
        "supporting-notes",
        "Supporting Notes\nUnchanged annex context for the contract.\n",
    )
    _advance_to_review(client, update_run_id, rule_set_id, rule_ids)
    update_review = _review_findings_mixed(client, update_run_id)
    change_set = update_review["update_change_set"]
    assert change_set is not None
    assert change_set["decision"] is None

    stale = client.post(
        f"/api/v1/runs/{update_run_id}/update/decision",
        json={
            "decision": "APPROVED",
            "actor_type": "HUMAN",
            "actor_id": "rest-e2e@example.test",
            "idempotency_key": f"update-review:{update_run_id}:stale",
            "expected_change_set_fingerprint": "sha256:" + "0" * 64,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "UPDATE_REVIEW_STALE"

    approved = client.post(
        f"/api/v1/runs/{update_run_id}/update/decision",
        json={
            "decision": "APPROVED",
            "actor_type": "HUMAN",
            "actor_id": "rest-e2e@example.test",
            "idempotency_key": f"update-review:{update_run_id}:approve",
            "expected_change_set_fingerprint": change_set["change_set_fingerprint"],
        },
    )
    assert approved.status_code == 200, approved.text

    ready = client.get(f"/api/v1/runs/{update_run_id}/review").json()
    assert ready["review_complete"] is True
    assert ready["commit_ready"] is True

    update_report, update_content = _commit_and_read_report(
        client,
        update_run_id,
        rule_set_id,
        rule_ids,
    )
    proof = update_report["focused_update"]
    assert proof is not None
    assert proof["baseline_run_id"] == initial_run_id
    assert proof["preserved_parts"]
    assert update_content != initial_content


def test_rest_failure_envelopes_are_black_box_stable(client: TestClient) -> None:
    pile_id, _rule_set_id, _rule_ids = _create_pile_and_rules(client)

    report_not_ready = client.get(f"/api/v1/runs/{'0' * 8}-0000-4000-8000-{'0' * 12}/report")
    assert report_not_ready.status_code == 404
    assert report_not_ready.json()["error"]["code"] == "RUN_NOT_FOUND"

    unsupported = client.post(
        f"/api/v1/piles/{pile_id}/documents",
        data={"logical_key": "binary"},
        files={"file": ("binary.bin", b"\xff\xfe\x00\x01", "application/octet-stream")},
    )
    assert unsupported.status_code == 415
    assert unsupported.json()["error"]["code"] == "UNSUPPORTED_SOURCE"
