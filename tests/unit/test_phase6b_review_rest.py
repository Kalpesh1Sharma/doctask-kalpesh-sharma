from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.application.external_advance_run_service import ExternalHumanReviewRequiredError
from api.application.external_update_review_service import ExternalUpdateReviewStaleError
from api.application.review_decision_service import RunNotReviewableError
from api.rest.dependencies import (
    get_external_advance_run_service,
    get_external_review_query_service,
    get_external_update_review_service,
    get_review_decision_service,
)
from api.rest.errors import install_error_handlers
from api.rest.router import api_v1_router
from domain.finding import FindingStatus, FindingType
from domain.review_decision import ReviewDecisionType
from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_review import UpdateChangeItemKind, UpdateReviewDecisionType

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_RUN_ID = "11111111-1111-1111-1111-111111111111"
_FINDING_ID = "22222222-2222-2222-2222-222222222222"
_DECISION_ID = "33333333-3333-3333-3333-333333333333"
_CHANGE_SET_ID = "44444444-4444-4444-4444-444444444444"
_OUTPUT_ID = "55555555-5555-5555-5555-555555555555"
_FINGERPRINT = "sha256:" + "a" * 64
_CONTENT_HASH = "sha256:" + "b" * 64


def _dependency_override(replacement: object):
    def override():
        return replacement

    return override


def _app(overrides: dict[object, object]) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(api_v1_router)
    for dependency, replacement in overrides.items():
        app.dependency_overrides[dependency] = _dependency_override(replacement)
    return app


class _ReviewQuery:
    def get_review(self, run_id: str):
        assert run_id == _RUN_ID
        evidence = SimpleNamespace(
            evidence_id="66666666-6666-6666-6666-666666666666",
            document_version_id="77777777-7777-7777-7777-777777777777",
            chunk_id="88888888-8888-8888-8888-888888888888",
            page_number=2,
            start_offset=10,
            end_offset=20,
            source_text="payment is due in 60 days",
            content_hash="sha256:" + "c" * 64,
        )
        decision = SimpleNamespace(
            review_decision_id=_DECISION_ID,
            finding_id=_FINDING_ID,
            decision=ReviewDecisionType.APPROVED,
            actor_type="HUMAN",
            actor_id="reviewer@example.test",
            idempotency_key=f"review:{_RUN_ID}:one",
            decided_at=_NOW,
        )
        finding = SimpleNamespace(
            run_id=_RUN_ID,
            finding_id=_FINDING_ID,
            relation=RunMembershipRelation.CREATED,
            origin_run_id=_RUN_ID,
            finding_type=FindingType.RULE_VIOLATION,
            rule_id="99999999-9999-9999-9999-999999999999",
            title="Payment term too long",
            description="60 exceeds 45",
            severity="HIGH",
            status=FindingStatus.APPROVED,
            evidence=(evidence,),
            decision=decision,
        )
        update_decision = SimpleNamespace(
            update_review_decision_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            change_set_id=_CHANGE_SET_ID,
            decision=UpdateReviewDecisionType.APPROVED,
            actor_type="HUMAN",
            actor_id="reviewer@example.test",
            idempotency_key=f"update-review:{_RUN_ID}:one",
            decided_at=_NOW,
        )
        update_evidence = SimpleNamespace(
            evidence_id="12121212-1212-4212-8212-121212121212",
            document_version_id="13131313-1313-4313-8313-131313131313",
            logical_key="loan_estimate",
            chunk_id="14141414-1414-4414-8414-141414141414",
            page_number=None,
            start_offset=10,
            end_offset=31,
            source_text="Interest Rate: 6.250%",
            content_hash="sha256:" + "d" * 64,
        )
        update_fact = SimpleNamespace(
            fact_id="15151515-1515-4515-8515-151515151515",
            subject="Loan Estimate",
            predicate="Interest Rate",
            object_value="6.250%",
            display_value="6.250%",
            status="CONFIRMED",
            concept="interest rate",
            evidence=(update_evidence,),
        )
        baseline_fact = SimpleNamespace(
            fact_id="16161616-1616-4616-8616-161616161616",
            subject="Interest Rate",
            predicate="is",
            object_value="6.375%",
            display_value="6.375%",
            status="CONFIRMED",
            concept="interest rate",
            evidence=(),
        )
        update_item = SimpleNamespace(
            item_id="17171717-1717-4717-8717-171717171717",
            fact_id=update_fact.fact_id,
            kind=UpdateChangeItemKind.FACT_CREATED,
            payload={"fact_id": update_fact.fact_id, "kind": "CREATED"},
            fact=update_fact,
            baseline_facts=(baseline_fact,),
            decision=None,
        )
        change_set = SimpleNamespace(
            change_set_id=_CHANGE_SET_ID,
            durable=True,
            change_set_fingerprint=_FINGERPRINT,
            manifest={"format": "update-change-set-v1", "run_id": _RUN_ID},
            items=(update_item,),
            decision=update_decision,
        )
        return SimpleNamespace(
            run_id=_RUN_ID,
            run_type=RunType.UPDATE,
            run_status=RunStatus.REVIEW_REQUIRED,
            findings=(finding,),
            finding_review_complete=True,
            update_change_set=change_set,
            review_complete=True,
            commit_ready=True,
        )

    def get_report(self, run_id: str):
        assert run_id == _RUN_ID
        return self._report()

    def read_report_content(self, run_id: str):
        assert run_id == _RUN_ID
        return self._report(), b"# Final Report\n\nexact bytes\n"

    @staticmethod
    def _report():
        return SimpleNamespace(
            output_id=_OUTPUT_ID,
            run_id=_RUN_ID,
            output_type="FINAL_REPORT",
            storage_uri="artifact://sha256/" + "b" * 64,
            content_hash=_CONTENT_HASH,
            created_at=_NOW,
            render_input_fingerprint=_FINGERPRINT,
            focused_update={"baseline_run_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
        )


class _FindingReview:
    command = None

    def record(self, command):
        self.command = command
        decision = SimpleNamespace(
            review_decision_id=_DECISION_ID,
            finding_id=command.finding_id,
            decision=command.decision,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            idempotency_key=command.idempotency_key,
            created_at=_NOW,
        )
        return SimpleNamespace(
            review_decision=decision,
            finding=SimpleNamespace(status=FindingStatus.REJECTED),
            review_complete=True,
            replayed=False,
        )


class _UpdateReview:
    command = None

    def record(self, command):
        self.command = command
        change_set = SimpleNamespace(
            change_set_id=_CHANGE_SET_ID,
            change_set_fingerprint=_FINGERPRINT,
        )
        decision = SimpleNamespace(
            decision=command.decision,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            idempotency_key=command.idempotency_key,
            decided_at=_NOW,
        )
        return SimpleNamespace(
            review=SimpleNamespace(
                change_set=change_set,
                decision=decision,
                replayed=False,
            ),
        )


class _ItemUpdateReview:
    command = None

    def record(self, command):
        self.command = command
        change_set = SimpleNamespace(
            change_set_id=_CHANGE_SET_ID,
            change_set_fingerprint=_FINGERPRINT,
        )
        item = SimpleNamespace(
            fact_id=_FINDING_ID,
            kind=UpdateChangeItemKind.FACT_CREATED,
        )
        decision = SimpleNamespace(
            decision=command.decision,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            idempotency_key=command.idempotency_key,
            decided_at=_NOW,
        )
        return SimpleNamespace(
            review=None,
            item_review=SimpleNamespace(
                change_set=change_set,
                item=item,
                decision=decision,
                review_complete=False,
                replayed=False,
            ),
        )


class _NotReviewable:
    def record(self, _command):
        raise RunNotReviewableError("run is not awaiting human review")


class _StaleUpdate:
    def record(self, _command):
        raise ExternalUpdateReviewStaleError(
            "UPDATE change set changed after the caller reviewed it",
        )


class _BlockedCommitWorkflow:
    def advance(self, _command):
        raise ExternalHumanReviewRequiredError("Run cannot enter COMMIT before review")


def test_review_get_exposes_evidence_decisions_and_update_approval() -> None:
    app = _app({get_external_review_query_service: _ReviewQuery()})

    with TestClient(app) as client:
        response = client.get(f"/api/v1/runs/{_RUN_ID}/review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["commit_ready"] is True
    assert payload["findings"][0]["evidence"][0]["page_number"] == 2
    assert payload["findings"][0]["decision"]["decision"] == "APPROVED"
    assert payload["update_change_set"]["change_set_fingerprint"] == _FINGERPRINT
    assert payload["update_change_set"]["decision"]["decision"] == "APPROVED"
    item = payload["update_change_set"]["items"][0]
    assert item["fact"]["concept"] == "interest rate"
    assert item["fact"]["evidence"][0]["logical_key"] == "loan_estimate"
    assert item["baseline_facts"][0]["object_value"] == "6.375%"
    assert item["baseline_facts"][0]["display_value"] == "6.375%"


def test_finding_decision_route_preserves_scoped_idempotency_and_actor() -> None:
    service = _FindingReview()
    app = _app({get_review_decision_service: service})
    key = f"review:{_RUN_ID}:reject-one"

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{_RUN_ID}/findings/{_FINDING_ID}/decision",
            json={
                "decision": "REJECTED",
                "actor_type": "HUMAN",
                "actor_id": "reviewer@example.test",
                "idempotency_key": key,
            },
        )

    assert response.status_code == 200
    assert response.json()["finding_status"] == "REJECTED"
    assert service.command.run_id == _RUN_ID
    assert service.command.finding_id == _FINDING_ID
    assert service.command.decision is ReviewDecisionType.REJECTED
    assert service.command.idempotency_key == key


def test_update_decision_requires_reviewed_change_set_fingerprint() -> None:
    service = _UpdateReview()
    app = _app({get_external_update_review_service: service})
    key = f"update-review:{_RUN_ID}:approve"

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{_RUN_ID}/update/decision",
            json={
                "decision": "APPROVED",
                "actor_type": "HUMAN",
                "actor_id": "reviewer@example.test",
                "idempotency_key": key,
                "expected_change_set_fingerprint": _FINGERPRINT,
            },
        )

    assert response.status_code == 200
    assert response.json()["change_set_fingerprint"] == _FINGERPRINT
    assert service.command.expected_change_set_fingerprint == _FINGERPRINT
    assert service.command.decision is UpdateReviewDecisionType.APPROVED


def test_report_content_returns_exact_bytes_and_hash_headers() -> None:
    app = _app({get_external_review_query_service: _ReviewQuery()})

    with TestClient(app) as client:
        metadata = client.get(f"/api/v1/runs/{_RUN_ID}/report")
        content = client.get(f"/api/v1/runs/{_RUN_ID}/report/content")

    assert metadata.status_code == 200
    assert metadata.json()["focused_update"]["baseline_run_id"].startswith("bbbb")
    assert content.status_code == 200
    assert content.content == b"# Final Report\n\nexact bytes\n"
    assert content.headers["etag"] == f'"{_CONTENT_HASH}"'
    assert content.headers["x-content-sha256"] == _CONTENT_HASH


def test_review_state_conflicts_use_stable_409_envelope() -> None:
    app = _app({get_review_decision_service: _NotReviewable()})

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{_RUN_ID}/findings/{_FINDING_ID}/decision",
            json={
                "decision": "APPROVED",
                "actor_type": "HUMAN",
                "actor_id": "reviewer@example.test",
                "idempotency_key": f"review:{_RUN_ID}:late",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_NOT_REVIEWABLE"


def test_stale_update_preview_is_rejected_with_409() -> None:
    app = _app({get_external_update_review_service: _StaleUpdate()})

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{_RUN_ID}/update/decision",
            json={
                "decision": "APPROVED",
                "actor_type": "HUMAN",
                "actor_id": "reviewer@example.test",
                "idempotency_key": f"update-review:{_RUN_ID}:stale",
                "expected_change_set_fingerprint": _FINGERPRINT,
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UPDATE_REVIEW_STALE"


def test_advance_cannot_bypass_pending_human_review() -> None:
    app = _app({get_external_advance_run_service: _BlockedCommitWorkflow()})

    with TestClient(app) as client:
        response = client.post(f"/api/v1/runs/{_RUN_ID}/advance", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HUMAN_REVIEW_REQUIRED"


def test_update_decision_can_target_one_material_fact_item() -> None:
    service = _ItemUpdateReview()
    app = _app({get_external_update_review_service: service})
    key = f"update-item-review:{_RUN_ID}:{_FINDING_ID}:approve"

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{_RUN_ID}/update/decision",
            json={
                "decision": "APPROVED",
                "actor_type": "HUMAN",
                "actor_id": "reviewer@example.test",
                "idempotency_key": key,
                "expected_change_set_fingerprint": _FINGERPRINT,
                "fact_id": _FINDING_ID,
            },
        )

    assert response.status_code == 200
    assert response.json()["fact_id"] == _FINDING_ID
    assert response.json()["item_kind"] == "FACT_CREATED"
    assert response.json()["review_complete"] is False
    assert service.command.fact_id == _FINDING_ID
