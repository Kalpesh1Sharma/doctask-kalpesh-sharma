from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from api.rest.app import create_app
from api.rest.errors import api_error_response, install_error_handlers
from api.rest.router import API_V1_PREFIX


class _RequiredPayload(BaseModel):
    count: int


def test_versioned_router_exposes_phase6b3_review_and_report_routes() -> None:
    app = create_app()
    paths = app.openapi()["paths"]

    assert API_V1_PREFIX == "/api/v1"
    assert "/health" in paths
    assert {
        "/api/v1/piles",
        "/api/v1/piles/{pile_id}",
        "/api/v1/piles/{pile_id}/documents",
        "/api/v1/piles/{pile_id}/rule-sets",
        "/api/v1/piles/{pile_id}/runs",
        "/api/v1/rule-sets/{rule_set_id}",
        "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/versions",
        "/api/v1/runs/{run_id}",
        "/api/v1/runs/{run_id}/advance",
        "/api/v1/runs/{run_id}/facts",
        "/api/v1/runs/{run_id}/findings",
        "/api/v1/runs/{run_id}/metrics",
        "/api/v1/piles/{pile_id}/change-history",
        "/api/v1/runs/{run_id}/change-history",
    }.issubset(paths)
    assert {
        "/api/v1/runs/{run_id}/review",
        "/api/v1/runs/{run_id}/findings/{finding_id}/decision",
        "/api/v1/runs/{run_id}/update/decision",
        "/api/v1/runs/{run_id}/report",
        "/api/v1/runs/{run_id}/report/content",
    }.issubset(paths)
    assert "/api/v1/runs/{run_id}/commit" not in paths


def test_request_validation_uses_stable_error_envelope() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/probe")
    def probe(payload: _RequiredPayload) -> dict[str, int]:
        return {"count": payload.count}

    with TestClient(app) as client:
        response = client.post("/probe", json={"count": "not-an-integer"})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert payload["error"]["message"] == "request validation failed"
    assert isinstance(payload["error"]["details"]["validation_errors"], list)


def test_api_error_response_always_has_object_details() -> None:
    response = api_error_response(409, "CONFLICT", "state conflict")

    assert response.status_code == 409
    assert b'"details":{}' in response.body


def test_rest_layer_does_not_import_repositories_or_execute_sql() -> None:
    rest_root = Path(__file__).resolve().parents[2] / "src" / "api" / "rest"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(rest_root.rglob("*.py"))
    )

    assert "from repositories" not in source
    assert "import repositories" not in source
    assert ".execute(" not in source
