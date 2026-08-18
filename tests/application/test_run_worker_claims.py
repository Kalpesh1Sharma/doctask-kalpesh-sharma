from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from config.settings import get_settings
from runtime.run_worker_claims import PostgresRunWorkerClaimCoordinator


def test_postgres_worker_claim_is_released_when_process_session_disappears() -> None:
    database_url = get_settings().database_url
    run_id = str(uuid4())
    first_worker = PostgresRunWorkerClaimCoordinator(database_url, owner_id="worker-a")
    second_worker = PostgresRunWorkerClaimCoordinator(database_url, owner_id="worker-b")

    first = first_worker.acquire(run_id)
    assert first is not None
    assert second_worker.acquire(run_id) is None

    # Simulate abrupt process/session death: no explicit advisory unlock occurs.
    first.connection.close()
    assert first_worker.is_owned(first) is False

    recovered = second_worker.acquire(run_id)
    assert recovered is not None
    assert recovered.owner_id == "worker-b"
    assert second_worker.release(recovered) is True


def test_postgres_worker_claim_allows_exactly_one_concurrent_owner_per_run() -> None:
    database_url = get_settings().database_url
    run_id = str(uuid4())
    start = Barrier(2, timeout=5)
    acquired = Barrier(2, timeout=5)

    def contend(owner_id: str) -> bool:
        coordinator = PostgresRunWorkerClaimCoordinator(database_url, owner_id=owner_id)
        start.wait()
        claim = coordinator.acquire(run_id)
        acquired.wait()
        if claim is not None:
            coordinator.release(claim)
            return True
        return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contend, ("worker-a", "worker-b")))

    assert results.count(True) == 1
