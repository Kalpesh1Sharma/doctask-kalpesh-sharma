from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest

from api.application.document_arrival_service import (
    DocumentArrivalService,
    PileTailIntegrityError,
)
from api.application.ingest_document_service import (
    DocumentIngestionClaimLostError,
    DocumentIngestionInProgressError,
    DocumentIngestionPreviouslyFailedError,
    DocumentIngestionProcessingError,
    IngestDocumentCommand,
    IngestDocumentResult,
    IngestDocumentService,
)
from db.pool import pool
from domain.document_version import DocumentProcessingStatus
from domain.run import RunType
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from repositories.pile_repository import PileRepository
from repositories.run_repository import RunRepository
from repositories.run_step_repository import RunStepRepository
from services.document_chunker import chunk_document
from services.document_parser import DocumentParser
from services.embedding_provider import DeterministicEmbeddingProvider
from services.local_source_storage import LocalSourceStorage


@pytest.fixture(scope="module", autouse=True)
def _open_pool() -> None:
    pool.open()


@pytest.fixture
def pile_id() -> str:
    with pool.connection() as connection:
        row = connection.execute(
            "INSERT INTO piles (name) VALUES (%s) RETURNING id",
            (f"phase6a document runtime {uuid4()}",),
        ).fetchone()
        assert row is not None
        value = str(row[0])
        connection.commit()
    try:
        yield value
    finally:
        with pool.connection() as connection:
            connection.execute(
                "UPDATE piles SET latest_run_id = NULL WHERE id = %s::uuid", (value,)
            )
            connection.execute(
                "DELETE FROM provider_usage_events WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (value,),
            )
            connection.execute(
                "DELETE FROM run_metrics WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (value,),
            )
            connection.execute(
                "DELETE FROM run_steps WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (value,),
            )
            connection.execute(
                "DELETE FROM run_document_versions WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)",
                (value,),
            )
            connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (value,))
            connection.execute(
                "DELETE FROM idempotency_records WHERE scope = 'document-ingestion-v1' AND key LIKE %s",
                (f"ingest:{value}:%",),
            )
            connection.execute(
                "UPDATE documents SET current_version_id = NULL WHERE pile_id = %s::uuid", (value,)
            )
            connection.execute(
                "DELETE FROM document_chunks WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))",
                (value,),
            )
            connection.execute(
                "DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid)",
                (value,),
            )
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (value,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (value,))
            connection.commit()


class _UnexpectedParser(DocumentParser):
    def parse_path(self, path: Path):
        raise AssertionError(f"parser must not run for replay: {path}")


class _UnexpectedEmbeddingProvider(DeterministicEmbeddingProvider):
    def embed(self, texts):
        raise AssertionError(f"embedding provider must not run for replay: {texts!r}")


class _BlockingParser(DocumentParser):
    def __init__(self, entered: Event, release: Event) -> None:
        self._entered = entered
        self._release = release

    def parse_path(self, path: Path):
        self._entered.set()
        assert self._release.wait(timeout=5), "timed out waiting to release ingestion parser"
        return super().parse_path(path)


def _backdate_ingestion_claim(key: str) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE idempotency_records
            SET claimed_at = clock_timestamp() - interval '10 minutes'
            WHERE scope = 'document-ingestion-v1' AND key = %s
            """,
            (key,),
        )
        connection.commit()


def _reclaimed_ingestion_claim(
    root: Path,
    pile_id: str,
    logical_key: str,
    content: bytes,
):
    storage = LocalSourceStorage(root)
    service = IngestDocumentService(
        pool,
        storage,
        DocumentParser(),
        DeterministicEmbeddingProvider(),
        stale_claim_after=timedelta(seconds=1),
    )
    source = storage.publish(content)
    original = service._claim_or_replay(pile_id, logical_key, source)
    assert not isinstance(original, IngestDocumentResult)
    _backdate_ingestion_claim(original.key)
    replacement = service._claim_or_replay(pile_id, logical_key, source)
    assert not isinstance(replacement, IngestDocumentResult)
    return service, original, replacement


def _ingester(root: Path, *, arrival: DocumentArrivalService | None = None) -> IngestDocumentService:
    return IngestDocumentService(
        pool,
        LocalSourceStorage(root),
        DocumentParser(),
        DeterministicEmbeddingProvider(),
        arrival_service=arrival,
    )


def test_ingestion_exact_replay_and_ready_version_are_durable(tmp_path: Path, pile_id: str) -> None:
    service = _ingester(tmp_path / "sources")

    created = service.ingest(IngestDocumentCommand(pile_id, "contract", b"alpha\n\nbeta"))
    replay = service.ingest(IngestDocumentCommand(pile_id, "contract", b"alpha\n\nbeta"))

    assert created.replayed is False
    assert replay.replayed is True
    assert replay.document_version_id == created.document_version_id
    with pool.connection() as connection:
        version = DocumentVersionRepository(connection).get_by_id(created.document_version_id)
        document = DocumentRepository(connection).get_by_id(created.document_id)
        record = IdempotencyRepository(connection).get(
            "document-ingestion-v1",
            service._key(pile_id, "contract", created.content_hash),
        )
        chunks = connection.execute(
            "SELECT count(*) FROM document_chunks WHERE document_version_id = %s::uuid",
            (created.document_version_id,),
        ).fetchone()
    assert version is not None and version.processing_status is DocumentProcessingStatus.READY
    assert document is not None and document.current_version_id == created.document_version_id
    assert record is not None and record.status is IdempotencyStatus.COMPLETED
    assert record.claim_generation == 1
    assert chunks == (1,)


def test_failed_identical_content_is_terminal_history(tmp_path: Path, pile_id: str) -> None:
    service = _ingester(tmp_path / "sources")
    with pytest.raises(DocumentIngestionProcessingError):
        service.ingest(IngestDocumentCommand(pile_id, "bad", b"\xff\xfe\x00"))

    replay_service = IngestDocumentService(
        pool,
        LocalSourceStorage(tmp_path / "sources"),
        _UnexpectedParser(),
        _UnexpectedEmbeddingProvider(),
    )
    with pytest.raises(DocumentIngestionPreviouslyFailedError):
        replay_service.ingest(IngestDocumentCommand(pile_id, "bad", b"\xff\xfe\x00"))

    with pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT processing_status FROM document_versions
            JOIN documents ON documents.id = document_versions.document_id
            WHERE documents.pile_id = %s::uuid AND documents.logical_key = 'bad'
            """,
            (pile_id,),
        ).fetchall()
    assert rows == [("FAILED",)]


def test_ready_arrival_creates_one_initial_and_duplicate_reconcile_is_harmless(
    tmp_path: Path, pile_id: str,
) -> None:
    arrival = DocumentArrivalService(pool)
    # Simulate a crash after both READY commits and before either arrival
    # callback.  Reconciliation must snapshot both into the one INITIAL.
    service = _ingester(tmp_path / "sources")
    first = service.ingest(IngestDocumentCommand(pile_id, "a", b"a body"))
    second = service.ingest(IngestDocumentCommand(pile_id, "b", b"b body"))

    # The first READY callback creates the INITIAL.  The next callback sees
    # both represented source versions and does not create a redundant UPDATE.
    first_arrival = arrival.reconcile_pile(pile_id)
    repeat = arrival.reconcile_pile(pile_id)
    with pool.connection() as connection:
        pile = PileRepository(connection).get_by_id(pile_id)
        runs = RunRepository(connection).list_for_pile(pile_id)
        assert pile is not None and pile.latest_run_id is not None
        assert len(runs) == 1 and runs[0].run_type is RunType.INITIAL
        scope_count = connection.execute(
            "SELECT count(*) FROM run_document_versions WHERE run_id = %s::uuid", (runs[0].run_id,)
        ).fetchone()
        steps = RunStepRepository(connection).list_for_run(runs[0].run_id)
    assert first.replayed is False and second.replayed is False
    assert len(first_arrival.created_run_ids) == 1
    assert repeat.created_run_ids == ()
    assert scope_count == (2,)
    assert {(step.stage.value, step.attempt) for step in steps} == {
        ("INGEST", 1), ("UNDERSTAND", 1), ("EXAMINE", 1), ("REVIEW", 1), ("COMMIT", 1),
    }


def test_missed_same_document_versions_coalesce_to_current_initial_scope(
    tmp_path: Path, pile_id: str,
) -> None:
    service = _ingester(tmp_path / "sources")
    older = service.ingest(IngestDocumentCommand(pile_id, "same", b"version one"))
    current = service.ingest(IngestDocumentCommand(pile_id, "same", b"version two"))

    result = DocumentArrivalService(pool).reconcile_pile(pile_id)
    assert len(result.created_run_ids) == 1
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT document_version_id FROM run_document_versions WHERE run_id = %s::uuid",
            (result.created_run_ids[0],),
        ).fetchone()
    assert row is not None and str(row[0]) == current.document_version_id
    assert older.document_version_id != current.document_version_id


def test_arrival_fails_closed_for_preexisting_runs_without_authoritative_tail(pile_id: str) -> None:
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO runs (pile_id, run_type, status) VALUES (%s::uuid, 'MANUAL', 'PENDING')",
            (pile_id,),
        )
        connection.commit()

    with pytest.raises(PileTailIntegrityError):
        DocumentArrivalService(pool).reconcile_pile(pile_id)


def test_later_ready_version_creates_blocked_update_from_nonterminal_tail(
    tmp_path: Path, pile_id: str,
) -> None:
    arrival = DocumentArrivalService(pool)
    service = _ingester(tmp_path / "sources", arrival=arrival)
    initial_source = service.ingest(IngestDocumentCommand(pile_id, "a", b"first"))
    second_source = service.ingest(IngestDocumentCommand(pile_id, "a", b"second"))

    with pool.connection() as connection:
        pile = PileRepository(connection).get_by_id(pile_id)
        runs = RunRepository(connection).list_for_pile(pile_id)
    assert pile is not None and len(runs) == 2
    update = next(run for run in runs if run.run_type is RunType.UPDATE)
    assert update.trigger_document_version_id == second_source.document_version_id
    assert update.blocked_on_run_id is not None
    assert update.baseline_run_id is None
    assert update.parent_run_id == update.blocked_on_run_id
    assert initial_source.document_version_id != second_source.document_version_id


def test_fresh_pending_ingestion_claim_reports_in_progress_without_provider_work(
    tmp_path: Path, pile_id: str,
) -> None:
    storage = LocalSourceStorage(tmp_path / "sources")
    owner = IngestDocumentService(
        pool, storage, DocumentParser(), DeterministicEmbeddingProvider(),
    )
    source = storage.publish(b"fresh pending claim")
    claim = owner._claim_or_replay(pile_id, "pending", source)
    assert not isinstance(claim, IngestDocumentResult)

    observer = IngestDocumentService(
        pool, storage, _UnexpectedParser(), _UnexpectedEmbeddingProvider(),
    )
    with pytest.raises(DocumentIngestionInProgressError):
        observer.ingest(IngestDocumentCommand(pile_id, "pending", b"fresh pending claim"))

    with pool.connection() as connection:
        version = DocumentVersionRepository(connection).get_by_id(claim.version.document_version_id)
        record = IdempotencyRepository(connection).get("document-ingestion-v1", claim.key)
    assert version is not None and version.processing_status is DocumentProcessingStatus.PENDING
    assert record is not None and record.status is IdempotencyStatus.IN_PROGRESS
    assert record.claim_generation == 1


def test_stale_pending_ingestion_claim_is_reclaimed_with_next_generation(
    tmp_path: Path, pile_id: str,
) -> None:
    _, original, replacement = _reclaimed_ingestion_claim(
        tmp_path / "sources", pile_id, "stale", b"stale pending claim",
    )

    assert original.generation == 1
    assert replacement.generation == 2
    assert replacement.version.document_version_id == original.version.document_version_id
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get("document-ingestion-v1", original.key)
        version = DocumentVersionRepository(connection).get_by_id(original.version.document_version_id)
    assert record is not None and record.status is IdempotencyStatus.IN_PROGRESS
    assert record.claim_generation == 2
    assert version is not None and version.processing_status is DocumentProcessingStatus.PENDING


def test_stale_ingestion_owner_cannot_mark_replacement_owners_version_ready(
    tmp_path: Path, pile_id: str,
) -> None:
    service, original, replacement = _reclaimed_ingestion_claim(
        tmp_path / "sources", pile_id, "stale-ready", b"stale ready fencing",
    )
    parsed = DocumentParser().parse_path(original.source.path)
    chunks = chunk_document(parsed)
    vectors = DeterministicEmbeddingProvider().embed(tuple(chunk.content for chunk in chunks))

    with pytest.raises(DocumentIngestionClaimLostError):
        service._persist_ready(original, parsed.mime_type, chunks, vectors)

    with pool.connection() as connection:
        version = DocumentVersionRepository(connection).get_by_id(original.version.document_version_id)
        document = DocumentRepository(connection).get_by_id(original.document.document_id)
        record = IdempotencyRepository(connection).get("document-ingestion-v1", original.key)
    assert replacement.generation == 2
    assert version is not None and version.processing_status is DocumentProcessingStatus.PENDING
    assert document is not None and document.current_version_id is None
    assert record is not None and record.status is IdempotencyStatus.IN_PROGRESS
    assert record.claim_generation == replacement.generation


def test_stale_ingestion_owner_chunk_writes_roll_back_when_claim_is_lost(
    tmp_path: Path, pile_id: str,
) -> None:
    service, original, _ = _reclaimed_ingestion_claim(
        tmp_path / "sources", pile_id, "stale-chunks", b"staged chunks must roll back",
    )
    parsed = DocumentParser().parse_path(original.source.path)
    chunks = chunk_document(parsed)
    assert chunks
    vectors = DeterministicEmbeddingProvider().embed(tuple(chunk.content for chunk in chunks))

    with pytest.raises(DocumentIngestionClaimLostError):
        service._persist_ready(original, parsed.mime_type, chunks, vectors)

    with pool.connection() as connection:
        persisted = connection.execute(
            "SELECT count(*) FROM document_chunks WHERE document_version_id = %s::uuid",
            (original.version.document_version_id,),
        ).fetchone()
        metadata = connection.execute(
            "SELECT mime_type, processing_status FROM document_versions WHERE id = %s::uuid",
            (original.version.document_version_id,),
        ).fetchone()
    assert persisted == (0,)
    assert metadata == (None, "PENDING")


def test_stale_ingestion_owner_cannot_mark_replacement_owners_version_failed(
    tmp_path: Path, pile_id: str,
) -> None:
    service, original, replacement = _reclaimed_ingestion_claim(
        tmp_path / "sources", pile_id, "stale-failed", b"stale failed fencing",
    )

    with pytest.raises(DocumentIngestionClaimLostError):
        service._mark_failed(original)

    with pool.connection() as connection:
        version = DocumentVersionRepository(connection).get_by_id(original.version.document_version_id)
        record = IdempotencyRepository(connection).get("document-ingestion-v1", original.key)
    assert version is not None and version.processing_status is DocumentProcessingStatus.PENDING
    assert record is not None and record.status is IdempotencyStatus.IN_PROGRESS
    assert record.claim_generation == replacement.generation == 2


def test_concurrent_identical_uploads_create_one_version_and_one_fixed_ingestion_identity(
    tmp_path: Path, pile_id: str,
) -> None:
    entered = Event()
    release = Event()
    root = tmp_path / "sources"
    service = IngestDocumentService(
        pool,
        LocalSourceStorage(root),
        _BlockingParser(entered, release),
        DeterministicEmbeddingProvider(),
    )
    content = b"concurrent identical upload"

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(
            service.ingest, IngestDocumentCommand(pile_id, "identical", content),
        )
        assert entered.wait(timeout=5), "owner did not reach parser after claiming ingestion"
        observer = IngestDocumentService(
            pool, LocalSourceStorage(root), _UnexpectedParser(), _UnexpectedEmbeddingProvider(),
        )
        with pytest.raises(DocumentIngestionInProgressError):
            observer.ingest(IngestDocumentCommand(pile_id, "identical", content))
        release.set()
        result = owner.result(timeout=5)

    with pool.connection() as connection:
        document = DocumentRepository(connection).get_by_logical_key(pile_id, "identical")
        assert document is not None
        versions = DocumentVersionRepository(connection).list_for_document(document.document_id)
        key = service._key(pile_id, "identical", result.content_hash)
        identities = connection.execute(
            "SELECT count(*) FROM idempotency_records WHERE scope = 'document-ingestion-v1' AND key = %s",
            (key,),
        ).fetchone()
    assert len(versions) == 1
    assert versions[0].document_version_id == result.document_version_id
    assert identities == (1,)


def test_concurrent_different_content_allocates_contiguous_unique_version_numbers(
    tmp_path: Path, pile_id: str,
) -> None:
    root = tmp_path / "sources"
    storage = LocalSourceStorage(root)
    service = IngestDocumentService(
        pool, storage, DocumentParser(), DeterministicEmbeddingProvider(),
    )
    barrier = Barrier(2)

    def claim(content: bytes):
        source = storage.publish(content)
        barrier.wait(timeout=5)
        return service._claim_or_replay(pile_id, "versions", source)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, b"version-A"), executor.submit(claim, b"version-B")]
        claims = [future.result(timeout=5) for future in futures]

    assert all(not isinstance(claimed, IngestDocumentResult) for claimed in claims)
    with pool.connection() as connection:
        document = DocumentRepository(connection).get_by_logical_key(pile_id, "versions")
        assert document is not None
        versions = DocumentVersionRepository(connection).list_for_document(document.document_id)
        identities = connection.execute(
            """
            SELECT count(*)
            FROM idempotency_records
            WHERE scope = 'document-ingestion-v1'
              AND key LIKE %s
            """,
            (f"ingest:{pile_id}:%",),
        ).fetchone()
    assert [version.version_number for version in versions] == [1, 2]
    assert len({version.document_version_id for version in versions}) == 2
    assert len({version.content_hash for version in versions}) == 2
    assert identities == (2,)


def test_ready_exact_replay_does_not_call_parser_or_embedding_provider(
    tmp_path: Path, pile_id: str,
) -> None:
    root = tmp_path / "sources"
    original = _ingester(root).ingest(
        IngestDocumentCommand(pile_id, "ready-replay", b"already ready"),
    )
    replay_service = IngestDocumentService(
        pool, LocalSourceStorage(root), _UnexpectedParser(), _UnexpectedEmbeddingProvider(),
    )

    replay = replay_service.ingest(
        IngestDocumentCommand(pile_id, "ready-replay", b"already ready"),
    )

    assert replay.replayed is True
    assert replay.document_version_id == original.document_version_id


def test_orphan_published_source_bytes_do_not_imply_ready_database_state(
    tmp_path: Path, pile_id: str,
) -> None:
    root = tmp_path / "sources"
    storage = LocalSourceStorage(root)
    orphan = storage.publish(b"published but not ingested")

    with pool.connection() as connection:
        documents = connection.execute(
            "SELECT count(*) FROM documents WHERE pile_id = %s::uuid", (pile_id,),
        ).fetchone()
        versions = connection.execute(
            """
            SELECT count(*)
            FROM document_versions
            JOIN documents ON documents.id = document_versions.document_id
            WHERE documents.pile_id = %s::uuid
            """,
            (pile_id,),
        ).fetchone()
        identities = connection.execute(
            "SELECT count(*) FROM idempotency_records WHERE scope = 'document-ingestion-v1' AND key LIKE %s",
            (f"ingest:{pile_id}:%",),
        ).fetchone()
    assert orphan.path.is_file()
    assert documents == (0,)
    assert versions == (0,)
    assert identities == (0,)

    result = IngestDocumentService(
        pool, storage, DocumentParser(), DeterministicEmbeddingProvider(),
    ).ingest(IngestDocumentCommand(pile_id, "orphan", b"published but not ingested"))
    with pool.connection() as connection:
        version = DocumentVersionRepository(connection).get_by_id(result.document_version_id)
    assert result.replayed is False
    assert version is not None and version.processing_status is DocumentProcessingStatus.READY
