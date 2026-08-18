from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.application.change_history_service import (
    ChangeHistoryIntegrityError,
    ChangeHistoryPileNotFoundError,
)
from api.rest.dependencies import get_change_history_service
from api.rest.errors import install_error_handlers
from api.rest.router import api_v1_router
from domain.change_history import ChangeHistoryOutcome
from domain.document_classification import DocumentRole
from domain.fact import FactStatus
from domain.update_preparation import UpdateDocumentChangeKind
from domain.update_review import UpdateChangeItemKind, UpdateReviewDecisionType

_NOW = datetime(2026, 8, 17, 18, 30, tzinfo=UTC)
_PILE_ID = "11111111-1111-4111-8111-111111111111"
_RUN_ID = "22222222-2222-4222-8222-222222222222"
_BASELINE_RUN_ID = "33333333-3333-4333-8333-333333333333"
_CHANGE_SET_ID = "44444444-4444-4444-8444-444444444444"
_DOCUMENT_ID = "55555555-5555-4555-8555-555555555555"
_VERSION_ID = "66666666-6666-4666-8666-666666666666"
_FACT_ID = "77777777-7777-4777-8777-777777777777"
_ITEM_ID = "88888888-8888-4888-8888-888888888888"
_EVIDENCE_ID = "99999999-9999-4999-8999-999999999999"
_DECISION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_OUTPUT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _history_run() -> SimpleNamespace:
    source = SimpleNamespace(
        pile_id=_PILE_ID,
        document_id=_DOCUMENT_ID,
        logical_key="closing-disclosure.pdf",
        document_version_id=_VERSION_ID,
        version_number=2,
        content_hash="sha256:" + "a" * 64,
        mime_type="application/pdf",
        role=DocumentRole.CLOSING_DISCLOSURE,
    )
    review = SimpleNamespace(
        decision_id=_DECISION_ID,
        decision=UpdateReviewDecisionType.APPROVED,
        actor_type="HUMAN",
        actor_id="reviewer@example.test",
        decided_at=_NOW,
    )
    fact = SimpleNamespace(
        fact_id=_FACT_ID,
        origin_run_id=_RUN_ID,
        subject="cash to close",
        predicate="amount",
        object_value=MappingProxyType(
            {"currency": "USD", "value": 1250, "tags": ("final",)},
        ),
        status=FactStatus.CONFIRMED,
    )
    evidence = SimpleNamespace(
        evidence_id=_EVIDENCE_ID,
        source=source,
        chunk_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        page_number=3,
        start_offset=120,
        end_offset=142,
        source_text="Cash to close: $1,250",
        content_hash="sha256:" + "d" * 64,
    )
    item = SimpleNamespace(
        change_item_id=_ITEM_ID,
        kind=UpdateChangeItemKind.FACT_CREATED,
        outcome=ChangeHistoryOutcome.APPLIED,
        fact=fact,
        review=review,
        evidence=(evidence,),
    )
    document_change = SimpleNamespace(
        document_id=_DOCUMENT_ID,
        kind=UpdateDocumentChangeKind.ADDED,
        baseline_source=None,
        target_source=source,
    )
    return SimpleNamespace(
        run_id=_RUN_ID,
        pile_id=_PILE_ID,
        baseline_run_id=_BASELINE_RUN_ID,
        change_set_id=_CHANGE_SET_ID,
        trigger_source=source,
        committed_at=_NOW,
        output_id=_OUTPUT_ID,
        output_content_hash="sha256:" + "e" * 64,
        commit_actor=SimpleNamespace(actor_type="SYSTEM", actor_id="task1-worker"),
        document_changes=(document_change,),
        items=(item,),
        whole_update_review=None,
    )


class _HistoryService:
    list_call: tuple[str, int] | None = None
    get_call: str | None = None

    def list_for_pile(self, pile_id: str, *, limit: int = 100):
        self.list_call = (pile_id, limit)
        return (_history_run(),)

    def get_for_run(self, run_id: str):
        self.get_call = run_id
        return _history_run()


class _MissingPileHistory:
    def list_for_pile(self, pile_id: str, *, limit: int = 100):
        raise ChangeHistoryPileNotFoundError(f"pile {pile_id} does not exist")


class _BrokenHistory:
    def get_for_run(self, run_id: str):
        raise ChangeHistoryIntegrityError(f"run {run_id} has inconsistent history")


def _app(service: object) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(api_v1_router)
    app.dependency_overrides[get_change_history_service] = lambda: service
    return app


def test_rest_lists_change_history_with_exact_source_review_and_commit_provenance() -> None:
    service = _HistoryService()
    app = _app(service)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/piles/{_PILE_ID}/change-history?limit=17")

    assert response.status_code == 200
    assert service.list_call == (_PILE_ID, 17)
    payload = response.json()[0]
    assert payload["run_id"] == _RUN_ID
    assert payload["committed_at"] == _NOW.isoformat().replace("+00:00", "Z")
    assert payload["trigger_source"]["logical_key"] == "closing-disclosure.pdf"
    assert payload["trigger_source"]["role"] == "CLOSING_DISCLOSURE"
    assert payload["document_changes"][0]["kind"] == "ADDED"
    assert payload["items"][0]["outcome"] == "APPLIED"
    assert payload["items"][0]["review"]["actor_id"] == "reviewer@example.test"
    assert payload["items"][0]["evidence"][0]["source_text"] == "Cash to close: $1,250"
    assert payload["items"][0]["fact"]["object_value"] == {
        "currency": "USD",
        "value": 1250,
        "tags": ["final"],
    }
    assert payload["commit_actor"] == {"actor_type": "SYSTEM", "actor_id": "task1-worker"}


def test_rest_gets_one_completed_update_change_history() -> None:
    service = _HistoryService()
    app = _app(service)

    with TestClient(app) as client:
        response = client.get(f"/api/v1/runs/{_RUN_ID}/change-history")

    assert response.status_code == 200
    assert service.get_call == _RUN_ID
    assert response.json()["change_set_id"] == _CHANGE_SET_ID


def test_rest_change_history_missing_pile_uses_stable_error_envelope() -> None:
    app = _app(_MissingPileHistory())

    with TestClient(app) as client:
        response = client.get(f"/api/v1/piles/{_PILE_ID}/change-history")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PILE_NOT_FOUND"


def test_rest_change_history_integrity_failure_does_not_leak_internal_details() -> None:
    app = _app(_BrokenHistory())

    with TestClient(app) as client:
        response = client.get(f"/api/v1/runs/{_RUN_ID}/change-history")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "CHANGE_HISTORY_INTEGRITY",
        "message": "durable state failed an integrity check",
        "details": {},
    }
