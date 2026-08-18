from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.application.external_query_service import PileNotFoundError
from api.rest.dependencies import (
    get_create_pile_service,
    get_create_rule_set_service,
    get_external_advance_run_service,
    get_external_query_service,
    get_ingest_and_reconcile_document_service,
)
from api.rest.errors import install_error_handlers
from api.rest.router import api_v1_router
from domain.run import RunStage, RunStatus, RunStepStatus

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_PILE_ID = "11111111-1111-1111-1111-111111111111"
_RULE_SET_ID = "22222222-2222-2222-2222-222222222222"
_RULE_ID = "33333333-3333-3333-3333-333333333333"
_DOCUMENT_ID = "44444444-4444-4444-4444-444444444444"
_VERSION_ID = "55555555-5555-5555-5555-555555555555"
_RUN_ID = "66666666-6666-6666-6666-666666666666"
_STEP_ID = "77777777-7777-7777-7777-777777777777"


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


class _CreatePile:
    command = None

    def create(self, command):
        self.command = command
        return SimpleNamespace(
            pile_id=_PILE_ID,
            name=command.name,
            description=command.description,
            watch_enabled=command.watch_enabled,
            watch_location=command.watch_location,
            latest_run_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )


class _CreateRuleSet:
    command = None

    def create(self, command):
        self.command = command
        rule_set = SimpleNamespace(
            rule_set_id=_RULE_SET_ID,
            pile_id=command.pile_id,
            name=command.name,
            description=command.description,
            version=command.version,
            created_at=_NOW,
        )
        rule = SimpleNamespace(
            rule_id=_RULE_ID,
            rule_set_id=_RULE_SET_ID,
            rule_key=command.rules[0].rule_key,
            title=command.rules[0].title,
            description=command.rules[0].description,
            severity=command.rules[0].severity,
            definition=command.rules[0].definition,
            created_at=_NOW,
        )
        return SimpleNamespace(rule_set=rule_set, rules=(rule,))


class _Ingest:
    source_bytes = b""

    def ingest(self, command):
        self.source_bytes = command.source.read()
        return SimpleNamespace(
            document_id=_DOCUMENT_ID,
            document_version_id=_VERSION_ID,
            content_hash="sha256:" + "a" * 64,
            storage_uri="source://sha256/" + "a" * 64,
            replayed=False,
            created_run_ids=(_RUN_ID,),
            reconciled_blocked_run_id=None,
            classification=SimpleNamespace(
                classification_id="88888888-8888-4888-8888-888888888888",
                document_version_id=_VERSION_ID,
                role="CLOSING_DISCLOSURE",
                confidence=0.98,
                explanation="Grounded classification.",
                evidence_chunk_ids=("99999999-9999-4999-8999-999999999999",),
                provider_name="groq",
                model_name="openai/gpt-oss-20b",
                input_tokens=20,
                output_tokens=5,
                replayed=False,
            ),
        )


class _Workflow:
    command = None

    def advance(self, command):
        self.command = command
        step = SimpleNamespace(
            step_id=_STEP_ID,
            run_id=command.run_id,
            stage=RunStage.INGEST,
            status=RunStepStatus.COMPLETED,
            attempt=1,
        )
        return SimpleNamespace(
            run_id=command.run_id,
            stage=RunStage.INGEST,
            step=step,
            run_status=RunStatus.RUNNING,
            outcome=SimpleNamespace(value="COMPLETED"),
            advanced=True,
            successor_attempt=None,
        )


class _MissingQuery:
    def get_pile(self, pile_id: str):
        raise PileNotFoundError(f"pile {pile_id} does not exist")


def test_create_pile_route_calls_application_service() -> None:
    service = _CreatePile()
    app = _app({get_create_pile_service: service})

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/piles",
            json={"name": "Acme", "description": "Contracts", "watch_enabled": False},
        )

    assert response.status_code == 201
    assert response.json()["pile_id"] == _PILE_ID
    assert service.command.name == "Acme"


def test_create_rule_set_route_preserves_structured_definition() -> None:
    service = _CreateRuleSet()
    app = _app({get_create_rule_set_service: service})

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/piles/{_PILE_ID}/rule-sets",
            json={
                "name": "Playbook",
                "version": 1,
                "rules": [
                    {
                        "rule_key": "payment",
                        "title": "Payment",
                        "severity": "HIGH",
                        "definition": {"max_days": 45},
                    },
                ],
            },
        )

    assert response.status_code == 201
    assert response.json()["rules"][0]["definition"] == {"max_days": 45}
    assert service.command.rules[0].definition == {"max_days": 45}


def test_multipart_ingestion_passes_exact_source_stream_to_facade() -> None:
    service = _Ingest()
    app = _app({get_ingest_and_reconcile_document_service: service})

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/piles/{_PILE_ID}/documents",
            data={"logical_key": "master-contract"},
            files={"file": ("contract.txt", b"hello contract", "text/plain")},
        )

    assert response.status_code == 201
    assert response.json()["created_run_ids"] == [_RUN_ID]
    assert response.json()["classification"]["role"] == "CLOSING_DISCLOSURE"
    assert service.source_bytes == b"hello contract"


def test_advance_route_preserves_optional_rule_selection() -> None:
    service = _Workflow()
    app = _app({get_external_advance_run_service: service})

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{_RUN_ID}/advance",
            json={
                "source_rule_set_ids": [_RULE_SET_ID],
                "source_rule_ids": [_RULE_ID],
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "run_id": _RUN_ID,
        "stage": "INGEST",
        "attempt": 1,
        "step_status": "COMPLETED",
        "run_status": "RUNNING",
        "outcome": "COMPLETED",
        "advanced": True,
        "successor_attempt": None,
    }
    assert service.command.source_rule_set_ids == (_RULE_SET_ID,)
    assert service.command.source_rule_ids == (_RULE_ID,)


def test_core_not_found_uses_stable_error_envelope() -> None:
    app = _app({get_external_query_service: _MissingQuery()})

    with TestClient(app) as client:
        response = client.get(f"/api/v1/piles/{_PILE_ID}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "PILE_NOT_FOUND",
            "message": f"pile {_PILE_ID} does not exist",
            "details": {},
        },
    }
