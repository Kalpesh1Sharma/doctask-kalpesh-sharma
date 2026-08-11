from fastapi.testclient import TestClient
from psycopg_pool import PoolTimeout

from api.rest.app import app
from db.health import check_database


class TimeoutPool:
    def connection(self) -> None:
        raise PoolTimeout()


def test_check_database_returns_false_when_pool_times_out(monkeypatch) -> None:
    monkeypatch.setattr("db.health.pool", TimeoutPool())

    assert check_database() is False


def test_health_reports_database_state(monkeypatch) -> None:
    with TestClient(app) as client:
        healthy_response = client.get("/health")
        monkeypatch.setattr("api.rest.app.check_database", lambda: False)
        unhealthy_response = client.get("/health")

    assert healthy_response.status_code == 200
    assert healthy_response.json() == {"status": "ok"}
    assert unhealthy_response.status_code == 200
    assert unhealthy_response.json() == {"status": "unhealthy"}
