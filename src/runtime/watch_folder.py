"""Polling watched-folder runtime for stable, idempotent document ingestion."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from psycopg_pool import ConnectionPool

from api.application.ingest_and_reconcile_document_service import (
    IngestAndReconcileDocumentCommand,
    IngestAndReconcileDocumentService,
)
from repositories.pile_repository import PileRepository

_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".txt"})
_LOG = logging.getLogger("task1.watch")


@dataclass(frozen=True)
class WatchedPile:
    pile_id: str
    watch_location: Path


class WatchFileStatus(StrEnum):
    INGESTED = "INGESTED"
    REPLAYED = "REPLAYED"
    IGNORED_UNSUPPORTED = "IGNORED_UNSUPPORTED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WatchFileOutcome:
    pile_id: str
    path: Path
    logical_key: str | None
    status: WatchFileStatus
    document_version_id: str | None = None
    document_role: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class WatchScanResult:
    pile_count: int
    outcomes: tuple[WatchFileOutcome, ...]

    @property
    def failures(self) -> tuple[WatchFileOutcome, ...]:
        return tuple(item for item in self.outcomes if item.status is WatchFileStatus.FAILED)


@dataclass(frozen=True)
class _FileSignature:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _Observation:
    signature: _FileSignature
    first_seen_at: float


class WatchFolderService:
    """Scan configured Pile watch locations without creating a second ingestion path.

    Files must remain unchanged for ``stable_after_seconds`` before being handed to
    ``IngestAndReconcileDocumentService``. Duplicate filesystem notifications are
    suppressed in-process, while the underlying durable ingestion/classification
    idempotency makes restart replays safe.
    """

    def __init__(
        self,
        ingestion_service: IngestAndReconcileDocumentService,
        pile_loader: Callable[[], tuple[WatchedPile, ...]],
        *,
        stable_after_seconds: float = 1.0,
        retry_delay_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if stable_after_seconds <= 0:
            raise ValueError("stable_after_seconds must be positive")
        if retry_delay_seconds <= 0:
            raise ValueError("retry_delay_seconds must be positive")
        self._ingestion = ingestion_service
        self._pile_loader = pile_loader
        self._stable_after_seconds = stable_after_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._clock = clock
        self._observations: dict[tuple[str, Path], _Observation] = {}
        self._processed: dict[tuple[str, Path], _FileSignature] = {}
        self._retry_after: dict[tuple[str, Path], float] = {}

    def scan_once(self) -> WatchScanResult:
        now = self._clock()
        piles = self._pile_loader()
        outcomes: list[WatchFileOutcome] = []
        active_keys: set[tuple[str, Path]] = set()

        for pile in piles:
            root = pile.watch_location.expanduser().resolve()
            try:
                root.mkdir(parents=True, exist_ok=True)
                if not root.is_dir():
                    raise NotADirectoryError(str(root))
                candidates = tuple(
                    sorted(
                        path
                        for path in root.rglob("*")
                        if path.is_file() and not path.is_symlink()
                    ),
                )
            except OSError as error:
                outcomes.append(
                    WatchFileOutcome(
                        pile_id=pile.pile_id,
                        path=root,
                        logical_key=None,
                        status=WatchFileStatus.FAILED,
                        error=f"watch location unavailable: {error}",
                    ),
                )
                continue

            for path in candidates:
                key = (pile.pile_id, path.resolve())
                active_keys.add(key)
                if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                    outcomes.append(
                        WatchFileOutcome(
                            pile_id=pile.pile_id,
                            path=path,
                            logical_key=None,
                            status=WatchFileStatus.IGNORED_UNSUPPORTED,
                        ),
                    )
                    continue
                try:
                    logical_key = self._logical_key(root, path)
                except ValueError as error:
                    outcomes.append(
                        WatchFileOutcome(
                            pile_id=pile.pile_id,
                            path=path,
                            logical_key=None,
                            status=WatchFileStatus.FAILED,
                            error=str(error),
                        ),
                    )
                    continue

                try:
                    signature = self._signature(path)
                except OSError as error:
                    outcomes.append(
                        WatchFileOutcome(
                            pile_id=pile.pile_id,
                            path=path,
                            logical_key=logical_key,
                            status=WatchFileStatus.FAILED,
                            error=f"source stat failed: {error}",
                        ),
                    )
                    continue

                if self._processed.get(key) == signature:
                    continue
                observed = self._observations.get(key)
                if observed is None or observed.signature != signature:
                    self._observations[key] = _Observation(signature, now)
                    self._retry_after.pop(key, None)
                    continue
                if now - observed.first_seen_at < self._stable_after_seconds:
                    continue
                if self._retry_after.get(key, 0.0) > now:
                    continue

                try:
                    result = self._ingestion.ingest(
                        IngestAndReconcileDocumentCommand(
                            pile_id=pile.pile_id,
                            logical_key=logical_key,
                            source=path,
                        ),
                    )
                    classification = result.classification
                    outcomes.append(
                        WatchFileOutcome(
                            pile_id=pile.pile_id,
                            path=path,
                            logical_key=logical_key,
                            status=(
                                WatchFileStatus.REPLAYED
                                if result.replayed
                                else WatchFileStatus.INGESTED
                            ),
                            document_version_id=result.document_version_id,
                            document_role=(
                                classification.role.value if classification is not None else None
                            ),
                        ),
                    )
                    try:
                        after = self._signature(path)
                    except OSError:
                        self._observations.pop(key, None)
                        self._processed.pop(key, None)
                    else:
                        if after == signature:
                            self._processed[key] = signature
                        else:
                            self._observations[key] = _Observation(after, now)
                            self._processed.pop(key, None)
                    self._retry_after.pop(key, None)
                except Exception as error:  # noqa: BLE001 - isolate one source from the runtime loop.
                    self._retry_after[key] = now + self._retry_delay_seconds
                    outcomes.append(
                        WatchFileOutcome(
                            pile_id=pile.pile_id,
                            path=path,
                            logical_key=logical_key,
                            status=WatchFileStatus.FAILED,
                            error=f"{type(error).__name__}: {error}",
                        ),
                    )

        for state in (self._observations, self._processed, self._retry_after):
            for key in tuple(state):
                if key not in active_keys:
                    state.pop(key, None)

        return WatchScanResult(len(piles), tuple(outcomes))

    @staticmethod
    def _logical_key(root: Path, path: Path) -> str:
        relative = path.relative_to(root)
        without_suffix = relative.with_suffix("")
        logical_key = without_suffix.as_posix().strip("/")
        if not logical_key or logical_key in {".", ".."}:
            raise ValueError("watched source path does not produce a logical document key")
        return logical_key

    @staticmethod
    def _signature(path: Path) -> _FileSignature:
        stat = path.stat()
        return _FileSignature(stat.st_size, stat.st_mtime_ns)


def postgres_watch_pile_loader(
    connection_pool: ConnectionPool,
    *,
    limit: int,
) -> Callable[[], tuple[WatchedPile, ...]]:
    def load() -> tuple[WatchedPile, ...]:
        with connection_pool.connection() as connection:
            records = PileRepository(connection).list_watch_enabled(limit)
            connection.commit()
        return tuple(
            WatchedPile(record.pile_id, Path(record.watch_location))
            for record in records
            if record.watch_location is not None
        )

    return load


def run_forever(
    service: WatchFolderService,
    *,
    poll_interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    stopper = stop_event or threading.Event()
    while not stopper.is_set():
        try:
            result = service.scan_once()
        except Exception:  # noqa: BLE001 - keep the daemon alive across transient DB/runtime faults.
            _LOG.exception("watch scan failed; retrying after poll interval")
            stopper.wait(poll_interval_seconds)
            continue
        for outcome in result.outcomes:
            if outcome.status is WatchFileStatus.FAILED:
                _LOG.error(
                    "watch failure pile=%s path=%s error=%s",
                    outcome.pile_id,
                    outcome.path,
                    outcome.error,
                )
            elif outcome.status is not WatchFileStatus.IGNORED_UNSUPPORTED:
                _LOG.info(
                    "watch %s pile=%s logical_key=%s version=%s role=%s",
                    outcome.status.value.lower(),
                    outcome.pile_id,
                    outcome.logical_key,
                    outcome.document_version_id,
                    outcome.document_role,
                )
        stopper.wait(poll_interval_seconds)


def main() -> None:
    from api.external_dependencies import get_ingest_and_reconcile_document_service
    from config.settings import get_settings
    from db.pool import pool

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    service = WatchFolderService(
        get_ingest_and_reconcile_document_service(),
        postgres_watch_pile_loader(pool, limit=settings.watch_pile_limit),
        stable_after_seconds=settings.watch_stable_after_seconds,
        retry_delay_seconds=settings.watch_retry_delay_seconds,
    )
    pool.open()
    try:
        run_forever(service, poll_interval_seconds=settings.watch_poll_interval_seconds)
    except KeyboardInterrupt:
        _LOG.info("watcher stopped")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
