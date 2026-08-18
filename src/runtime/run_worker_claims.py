from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from psycopg import Connection, Error, connect


@dataclass
class RunWorkerClaim:
    run_id: str
    owner_id: str
    lock_key: int
    connection: Connection = field(repr=False, compare=False)


class PostgresRunWorkerClaimCoordinator:
    """Transient PostgreSQL advisory coordination for background workers.

    Durable Run/RunStep/idempotency rows remain authoritative. The advisory lock
    only prevents multiple worker processes from routinely driving the same Run
    at once. PostgreSQL releases the lock automatically when the owning session
    closes, including after process death or database restart.
    """

    def __init__(self, database_url: str, *, owner_id: str) -> None:
        if not database_url.strip():
            raise ValueError("database_url must not be empty")
        if not owner_id.strip():
            raise ValueError("owner_id must not be empty")
        self._database_url = database_url
        self._owner_id = owner_id.strip()

    @staticmethod
    def _lock_key(run_id: str) -> int:
        identity = f"task1:run-worker:{run_id}".encode()
        return int.from_bytes(hashlib.sha256(identity).digest()[:8], "big", signed=True)

    def acquire(self, run_id: str) -> RunWorkerClaim | None:
        lock_key = self._lock_key(run_id)
        connection = connect(self._database_url, autocommit=True)
        try:
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s::bigint)",
                (lock_key,),
            ).fetchone()
            if row is None or not bool(row[0]):
                connection.close()
                return None
            return RunWorkerClaim(
                run_id=run_id,
                owner_id=self._owner_id,
                lock_key=lock_key,
                connection=connection,
            )
        except Error:
            connection.close()
            raise

    def is_owned(self, claim: RunWorkerClaim) -> bool:
        if claim.connection.closed:
            return False
        try:
            claim.connection.execute("SELECT 1").fetchone()
        except Error:
            claim.connection.close()
            return False
        return True

    def release(self, claim: RunWorkerClaim) -> bool:
        if claim.connection.closed:
            return False
        try:
            row = claim.connection.execute(
                "SELECT pg_advisory_unlock(%s::bigint)",
                (claim.lock_key,),
            ).fetchone()
            return row is not None and bool(row[0])
        finally:
            claim.connection.close()
