from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from api.application.classify_document_version_service import ClassifyDocumentVersionResult
from api.application.ingest_and_reconcile_document_service import (
    IngestAndReconcileDocumentResult,
)
from domain.document_classification import DocumentRole
from runtime.watch_folder import (
    WatchedPile,
    WatchFileStatus,
    WatchFolderService,
    WatchScanResult,
    run_forever,
)


@dataclass
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Ingestion:
    def __init__(self, *, fail_keys: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, Path]] = []
        self._seen: set[tuple[str, str, bytes]] = set()
        self._fail_keys = fail_keys or set()

    def ingest(self, command) -> IngestAndReconcileDocumentResult:
        path = Path(command.source)
        self.calls.append((command.pile_id, command.logical_key, path))
        if command.logical_key in self._fail_keys:
            raise RuntimeError("synthetic watcher failure")
        payload = path.read_bytes()
        identity = (command.pile_id, command.logical_key, payload)
        replayed = identity in self._seen
        self._seen.add(identity)
        classification = ClassifyDocumentVersionResult(
            classification_id="11111111-1111-4111-8111-111111111111",
            document_version_id="22222222-2222-4222-8222-222222222222",
            role=DocumentRole.CLOSING_DISCLOSURE,
            confidence=0.99,
            explanation="Grounded closing disclosure markers.",
            evidence_chunk_ids=("33333333-3333-4333-8333-333333333333",),
            provider_name="test",
            model_name="test-model",
            input_tokens=10,
            output_tokens=5,
            replayed=replayed,
        )
        return IngestAndReconcileDocumentResult(
            document_id="44444444-4444-4444-8444-444444444444",
            document_version_id=classification.document_version_id,
            content_hash="sha256:" + "a" * 64,
            storage_uri="source://sha256/" + "a" * 64,
            replayed=replayed,
            created_run_ids=(),
            reconciled_blocked_run_id=None,
            classification=classification,
        )


def _service(tmp_path: Path, ingestion: _Ingestion, clock: _Clock) -> tuple[WatchFolderService, Path]:
    root = tmp_path / "watch"
    service = WatchFolderService(
        ingestion,  # type: ignore[arg-type]
        lambda: (WatchedPile("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", root),),
        stable_after_seconds=1.0,
        retry_delay_seconds=5.0,
        clock=clock,
    )
    return service, root


def test_watcher_waits_for_stability_then_ingests_and_classifies(tmp_path: Path) -> None:
    ingestion = _Ingestion()
    clock = _Clock()
    service, root = _service(tmp_path, ingestion, clock)
    source = root / "closing" / "disclosure.txt"
    source.parent.mkdir(parents=True)
    source.write_text("Closing Disclosure\nLoan terms", encoding="utf-8")

    assert service.scan_once().outcomes == ()
    clock.advance(1.1)
    result = service.scan_once()

    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.status is WatchFileStatus.INGESTED
    assert outcome.logical_key == "closing/disclosure"
    assert outcome.document_role == "CLOSING_DISCLOSURE"
    assert ingestion.calls == [
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "closing/disclosure", source.resolve()),
    ]


def test_watcher_suppresses_unchanged_duplicate_events_but_processes_new_bytes(tmp_path: Path) -> None:
    ingestion = _Ingestion()
    clock = _Clock()
    service, root = _service(tmp_path, ingestion, clock)
    source = root / "note.txt"
    root.mkdir(parents=True)
    source.write_text("Promissory Note v1", encoding="utf-8")

    service.scan_once()
    clock.advance(1.1)
    assert service.scan_once().outcomes[0].status is WatchFileStatus.INGESTED
    clock.advance(20)
    assert service.scan_once().outcomes == ()
    assert len(ingestion.calls) == 1

    source.write_text("Promissory Note version two", encoding="utf-8")
    assert service.scan_once().outcomes == ()
    clock.advance(1.1)
    assert service.scan_once().outcomes[0].status is WatchFileStatus.INGESTED
    assert len(ingestion.calls) == 2


def test_watcher_ignores_unsupported_files_without_crashing(tmp_path: Path) -> None:
    ingestion = _Ingestion()
    clock = _Clock()
    service, root = _service(tmp_path, ingestion, clock)
    root.mkdir(parents=True)
    unsupported = root / "readme.csv"
    unsupported.write_text("not,a,source", encoding="utf-8")

    result = service.scan_once()

    assert len(result.outcomes) == 1
    assert result.outcomes[0].status is WatchFileStatus.IGNORED_UNSUPPORTED
    assert result.outcomes[0].path == unsupported
    assert ingestion.calls == []


def test_watcher_isolates_one_file_failure_and_honors_retry_delay(tmp_path: Path) -> None:
    ingestion = _Ingestion(fail_keys={"bad"})
    clock = _Clock()
    service, root = _service(tmp_path, ingestion, clock)
    root.mkdir(parents=True)
    (root / "bad.txt").write_text("bad", encoding="utf-8")
    (root / "good.txt").write_text("good", encoding="utf-8")

    service.scan_once()
    clock.advance(1.1)
    result = service.scan_once()

    by_key = {item.logical_key: item for item in result.outcomes}
    assert by_key["bad"].status is WatchFileStatus.FAILED
    assert by_key["good"].status is WatchFileStatus.INGESTED
    assert len(ingestion.calls) == 2

    clock.advance(1.0)
    service.scan_once()
    assert len(ingestion.calls) == 2
    clock.advance(4.1)
    retry = service.scan_once()
    assert retry.outcomes[0].logical_key == "bad"
    assert retry.outcomes[0].status is WatchFileStatus.FAILED
    assert len(ingestion.calls) == 3


def test_watcher_process_restart_exact_replays_durable_ingestion(tmp_path: Path) -> None:
    ingestion = _Ingestion()
    clock = _Clock()
    first, root = _service(tmp_path, ingestion, clock)
    source = root / "restart-safe.txt"
    root.mkdir(parents=True)
    source.write_text("Closing Disclosure restart replay", encoding="utf-8")

    first.scan_once()
    clock.advance(1.1)
    assert first.scan_once().outcomes[0].status is WatchFileStatus.INGESTED

    restarted, _ = _service(tmp_path, ingestion, clock)
    assert restarted.scan_once().outcomes == ()
    clock.advance(1.1)
    replay = restarted.scan_once()

    assert replay.outcomes[0].status is WatchFileStatus.REPLAYED
    assert replay.outcomes[0].logical_key == "restart-safe"
    assert len(ingestion.calls) == 2


def test_watcher_daemon_retries_after_transient_scan_failure() -> None:
    stop = threading.Event()

    class _RestartingService:
        calls = 0

        def scan_once(self) -> WatchScanResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database restarting")
            stop.set()
            return WatchScanResult(0, ())

    service = _RestartingService()
    run_forever(service, poll_interval_seconds=0.001, stop_event=stop)  # type: ignore[arg-type]

    assert service.calls == 2
