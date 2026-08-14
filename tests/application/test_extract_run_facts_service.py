import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import uuid4

import pytest
from psycopg.errors import DeadlockDetected, DivisionByZero, LockNotAvailable, SerializationFailure

from api.application import extract_run_facts_service as extraction_module
from api.application.extract_run_facts_service import (
    ExtractRunFactsCommand,
    ExtractRunFactsService,
    FactExtractionClaimLostError,
    FactExtractionInProgressError,
    FactExtractionInputConflictError,
    FactExtractionProviderFailure,
    FactExtractionRunNotEligibleError,
    SourceEvidenceValidationError,
    SourceInputDriftError,
)
from db.pool import pool
from domain.fact_extraction import (
    FactEvidenceCandidate,
    FactExtractionCandidate,
    FactExtractionRequest,
    FactExtractionResponse,
)
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from repositories.run_repository import RunRepository
from services.fact_extraction_provider import FactExtractionProviderError


@dataclass(frozen=True)
class ExtractionResources:
    pile_id: str
    run_id: str
    document_version_id: str
    chunk_id: str
    other_document_version_id: str
    other_chunk_id: str


class FakeProvider:
    def __init__(self, response: FactExtractionResponse) -> None:
        self.response = response
        self.calls: list[FactExtractionRequest] = []
        self.before_return = None

    def extract(self, request: FactExtractionRequest) -> FactExtractionResponse:
        self.calls.append(request)
        if self.before_return is not None:
            self.before_return(request)
        return self.response


class FailingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def extract(self, _: FactExtractionRequest) -> FactExtractionResponse:
        self.calls += 1
        raise FactExtractionProviderError("provider unavailable")


@pytest.fixture(scope="module", autouse=True)
def open_pool() -> None:
    pool.open()


@pytest.fixture
def resources() -> ExtractionResources:
    with pool.connection() as connection:
        pile_id = create_pile(connection)
        run_id = create_run(connection, pile_id, "RUNNING")
        document_version_id, chunk_id = create_document_version_with_chunk(
            connection,
            pile_id,
            "primary",
            "The amount is 100 USD.",
        )
        other_document_version_id, other_chunk_id = create_document_version_with_chunk(
            connection,
            pile_id,
            "outside",
            "Unattached source.",
        )
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (run_id, document_version_id),
        )
        connection.commit()

    created = ExtractionResources(
        pile_id,
        run_id,
        document_version_id,
        chunk_id,
        other_document_version_id,
        other_chunk_id,
    )
    try:
        yield created
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE scope = 'fact-extraction-v1' AND key = %s",
                (f"understand:{created.run_id}",),
            )
            connection.execute(
                "DELETE FROM fact_evidence WHERE fact_id IN (SELECT id FROM facts WHERE origin_run_id = %s::uuid)",
                (created.run_id,),
            )
            connection.execute(
                "DELETE FROM run_facts WHERE run_id = %s::uuid",
                (created.run_id,),
            )
            connection.execute(
                "DELETE FROM facts WHERE origin_run_id = %s::uuid",
                (created.run_id,),
            )
            connection.execute(
                "DELETE FROM evidence_spans WHERE document_version_id IN (%s::uuid, %s::uuid)",
                (created.document_version_id, created.other_document_version_id),
            )
            connection.execute(
                "DELETE FROM document_chunks WHERE document_version_id IN (%s::uuid, %s::uuid)",
                (created.document_version_id, created.other_document_version_id),
            )
            connection.execute(
                "DELETE FROM run_document_versions WHERE run_id = %s::uuid",
                (created.run_id,),
            )
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (created.run_id,))
            connection.execute(
                "DELETE FROM document_versions WHERE id IN (%s::uuid, %s::uuid)",
                (created.document_version_id, created.other_document_version_id),
            )
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created.pile_id,))
            connection.commit()


def create_pile(connection) -> str:
    row = connection.execute(
        "INSERT INTO piles (name) VALUES (%s) RETURNING id",
        (f"Fact extraction service {uuid4()}",),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_run(connection, pile_id: str, status: str) -> str:
    row = connection.execute(
        """
        INSERT INTO runs (pile_id, run_type, status)
        VALUES (%s::uuid, 'MANUAL'::run_type, %s::run_status)
        RETURNING id
        """,
        (pile_id, status),
    ).fetchone()
    assert row is not None
    return str(row[0])


def create_document_version_with_chunk(connection, pile_id: str, logical_key: str, content: str) -> tuple[str, str]:
    document = connection.execute(
        "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, %s) RETURNING id",
        (pile_id, logical_key),
    ).fetchone()
    assert document is not None
    version = connection.execute(
        """
        INSERT INTO document_versions (document_id, version_number, content_hash, storage_uri)
        VALUES (%s::uuid, 1, %s, %s)
        RETURNING id
        """,
        (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
    ).fetchone()
    assert version is not None
    chunk = connection.execute(
        """
        INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash, page_number)
        VALUES (%s::uuid, 0, %s, %s, 4)
        RETURNING id
        """,
        (str(version[0]), content, f"sha256:{uuid4().hex}"),
    ).fetchone()
    assert chunk is not None
    return str(version[0]), str(chunk[0])


def response_for(resources: ExtractionResources) -> FactExtractionResponse:
    return FactExtractionResponse(
        format="fact-extraction-v1",
        candidates=(
            FactExtractionCandidate(
                subject="invoice amount",
                predicate="equals",
                object_value={"amount": 100, "currency": "USD"},
                evidence=(
                    FactEvidenceCandidate(
                        chunk_id=resources.chunk_id,
                        relative_start_offset=14,
                        relative_end_offset=21,
                        quoted_text="100 USD",
                    ),
                ),
            ),
        ),
    )


def service(provider: FakeProvider) -> ExtractRunFactsService:
    return ExtractRunFactsService(
        pool,
        provider,
        "Extract grounded factual statements only.",
        "grounded-facts-v1",
    )


def grounded_counts(resources: ExtractionResources) -> tuple[int, int, int, int]:
    with pool.connection() as connection:
        facts = connection.execute(
            "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        memberships = connection.execute(
            "SELECT count(*) FROM run_facts WHERE run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        evidence = connection.execute(
            """
            SELECT count(*) FROM evidence_spans
            WHERE document_version_id IN (%s::uuid, %s::uuid)
            """,
            (resources.document_version_id, resources.other_document_version_id),
        ).fetchone()
        links = connection.execute(
            """
            SELECT count(*)
            FROM fact_evidence
            JOIN facts ON facts.id = fact_evidence.fact_id
            WHERE facts.origin_run_id = %s::uuid
            """,
            (resources.run_id,),
        ).fetchone()

    assert facts is not None
    assert memberships is not None
    assert evidence is not None
    assert links is not None
    return int(facts[0]), int(memberships[0]), int(evidence[0]), int(links[0])


def extraction_record(resources: ExtractionResources):
    with pool.connection() as connection:
        return IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{resources.run_id}",
        )


def test_extracts_grounded_fact_with_chunk_relative_evidence(resources: ExtractionResources) -> None:
    provider = FakeProvider(response_for(resources))

    result = service(provider).extract(ExtractRunFactsCommand(resources.run_id.upper()))

    assert result.replayed is False
    assert len(provider.calls) == 1
    assert [chunk.chunk_id for chunk in provider.calls[0].source_chunks] == [resources.chunk_id]
    with pool.connection() as connection:
        fact = connection.execute(
            "SELECT id, origin_run_id, subject, predicate, object_value, status FROM facts WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        assert fact is not None
        membership = connection.execute(
            "SELECT relation FROM run_facts WHERE run_id = %s::uuid AND fact_id = %s::uuid",
            (resources.run_id, str(fact[0])),
        ).fetchone()
        evidence = connection.execute(
            """
            SELECT document_version_id, chunk_id, page_number, start_offset, end_offset, quoted_text, content_hash
            FROM evidence_spans
            WHERE id = (SELECT evidence_span_id FROM fact_evidence WHERE fact_id = %s::uuid)
            """,
            (str(fact[0]),),
        ).fetchone()
        idempotency = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{resources.run_id}",
        )

    assert str(fact[1]) == resources.run_id
    assert fact[2:4] == ("invoice amount", "equals")
    assert fact[4] == {"amount": 100, "currency": "USD"}
    assert str(fact[5]) == "CONFIRMED"
    assert membership == ("CREATED",)
    assert evidence is not None
    assert tuple(map(str, evidence[:2])) == (resources.document_version_id, resources.chunk_id)
    assert evidence[2:6] == (4, 14, 21, "100 USD")
    assert evidence[6] == "sha256:4051c470645706772a31db464ccc3715319aa99630b799bd9d2199e8710c9076"
    assert idempotency is not None
    assert idempotency.status is IdempotencyStatus.COMPLETED
    assert idempotency.result_reference == f"fact-extraction:{resources.run_id}:{result.input_fingerprint}"


def test_completed_same_input_replays_and_changed_input_conflicts(resources: ExtractionResources) -> None:
    provider = FakeProvider(response_for(resources))
    extractor = service(provider)
    first = extractor.extract(ExtractRunFactsCommand(resources.run_id))
    replay = extractor.extract(ExtractRunFactsCommand(resources.run_id))

    assert replay.replayed is True
    assert replay.input_fingerprint == first.input_fingerprint
    assert len(provider.calls) == 1

    with pool.connection() as connection:
        connection.execute(
            "UPDATE document_chunks SET content = %s WHERE id = %s::uuid",
            ("The amount is 101 USD.", resources.chunk_id),
        )
        connection.commit()

    with pytest.raises(FactExtractionInputConflictError):
        extractor.extract(ExtractRunFactsCommand(resources.run_id))
    assert len(provider.calls) == 1


def test_extracts_multiple_facts_with_cross_document_and_shared_evidence(
    resources: ExtractionResources,
) -> None:
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO run_document_versions (run_id, document_version_id) VALUES (%s::uuid, %s::uuid)",
            (resources.run_id, resources.other_document_version_id),
        )
        connection.commit()

    provider = FakeProvider(
        FactExtractionResponse(
            format="fact-extraction-v1",
            candidates=(
                FactExtractionCandidate(
                    subject="invoice amount",
                    predicate="equals",
                    object_value=100,
                    evidence=(
                        FactEvidenceCandidate(
                            chunk_id=resources.chunk_id,
                            relative_start_offset=14,
                            relative_end_offset=21,
                            quoted_text="100 USD",
                        ),
                        FactEvidenceCandidate(
                            chunk_id=resources.other_chunk_id,
                            relative_start_offset=0,
                            relative_end_offset=10,
                            quoted_text="Unattached",
                        ),
                    ),
                ),
                FactExtractionCandidate(
                    subject="second source",
                    predicate="mentions",
                    object_value="Unattached",
                    evidence=(
                        FactEvidenceCandidate(
                            chunk_id=resources.other_chunk_id,
                            relative_start_offset=0,
                            relative_end_offset=10,
                            quoted_text="Unattached",
                        ),
                    ),
                ),
            ),
        ),
    )

    service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    assert {chunk.chunk_id for chunk in provider.calls[0].source_chunks} == {
        resources.chunk_id,
        resources.other_chunk_id,
    }
    with pool.connection() as connection:
        facts = connection.execute(
            "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        evidence = connection.execute(
            """
            SELECT count(*) FROM evidence_spans
            WHERE document_version_id IN (%s::uuid, %s::uuid)
            """,
            (resources.document_version_id, resources.other_document_version_id),
        ).fetchone()
        links = connection.execute(
            """
            SELECT count(*)
            FROM fact_evidence
            JOIN facts ON facts.id = fact_evidence.fact_id
            WHERE facts.origin_run_id = %s::uuid
            """,
            (resources.run_id,),
        ).fetchone()

    assert facts == (2,)
    assert evidence == (2,)
    assert links == (3,)


def test_source_drift_rejects_writes_and_leaves_claim_recoverable(resources: ExtractionResources) -> None:
    provider = FakeProvider(response_for(resources))

    def mutate_source(_: FactExtractionRequest) -> None:
        with pool.connection() as connection:
            connection.execute(
                "UPDATE document_chunks SET content = %s WHERE id = %s::uuid",
                ("The amount is 101 USD.", resources.chunk_id),
            )
            connection.commit()

    provider.before_return = mutate_source
    with pytest.raises(SourceInputDriftError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    with pool.connection() as connection:
        facts = connection.execute(
            "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{resources.run_id}",
        )

    assert facts == (0,)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS
    assert record.claimed_at is not None


def test_invalid_evidence_writes_no_facts_and_marks_claim_failed(resources: ExtractionResources) -> None:
    invalid = FactExtractionResponse(
        format="fact-extraction-v1",
        candidates=(
            FactExtractionCandidate(
                subject="amount",
                predicate="equals",
                object_value=100,
                evidence=(
                    FactEvidenceCandidate(
                        chunk_id=resources.other_chunk_id,
                        relative_start_offset=0,
                        relative_end_offset=4,
                        quoted_text="Unat",
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(SourceEvidenceValidationError):
        service(FakeProvider(invalid)).extract(ExtractRunFactsCommand(resources.run_id))

    with pool.connection() as connection:
        count = connection.execute(
            "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{resources.run_id}",
        )

    assert count == (0,)
    assert record is not None
    assert record.status is IdempotencyStatus.FAILED


def test_zero_fact_result_completes_without_fact_or_evidence_rows(resources: ExtractionResources) -> None:
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))
    result = service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    with pool.connection() as connection:
        facts = connection.execute(
            "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT count(*) FROM evidence_spans WHERE document_version_id = %s::uuid",
            (resources.document_version_id,),
        ).fetchone()

    assert result.replayed is False
    assert facts == (0,)
    assert evidence == (0,)


def test_in_progress_claim_prevents_provider_call(resources: ExtractionResources) -> None:
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(
            "fact-extraction-v1",
            f"understand:{resources.run_id}",
        )
        connection.commit()

    assert record is not None
    provider = FakeProvider(response_for(resources))
    with pytest.raises(FactExtractionInProgressError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))
    assert provider.calls == []


def test_fresh_extraction_requires_running_status(resources: ExtractionResources) -> None:
    with pool.connection() as connection:
        connection.execute("UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid", (resources.run_id,))
        connection.commit()

    with pytest.raises(FactExtractionRunNotEligibleError):
        service(FakeProvider(response_for(resources))).extract(ExtractRunFactsCommand(resources.run_id))


def test_provider_failure_marks_owned_claim_failed_without_grounded_rows(
    resources: ExtractionResources,
) -> None:
    provider = FailingProvider()

    with pytest.raises(FactExtractionProviderFailure):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    record = extraction_record(resources)
    assert provider.calls == 1
    assert grounded_counts(resources) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.FAILED


def test_database_failure_after_staging_rolls_back_grounded_rows_and_completion(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_mark_completed = extraction_module.IdempotencyRepository.mark_completed

    def fail_after_completion(repository, *args):
        original_mark_completed(repository, *args)
        repository._connection.execute("SELECT 1 / 0")
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        extraction_module.IdempotencyRepository,
        "mark_completed",
        fail_after_completion,
    )

    with pytest.raises(DivisionByZero):
        service(FakeProvider(response_for(resources))).extract(ExtractRunFactsCommand(resources.run_id))

    record = extraction_record(resources)
    assert grounded_counts(resources) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS
    assert record.completed_at is None


def test_run_progression_during_provider_call_rejects_persistence_without_writes(
    resources: ExtractionResources,
) -> None:
    provider = FakeProvider(response_for(resources))

    def require_review(_: FactExtractionRequest) -> None:
        with pool.connection() as connection:
            connection.execute(
                "UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid",
                (resources.run_id,),
            )
            connection.commit()

    provider.before_return = require_review
    with pytest.raises(FactExtractionRunNotEligibleError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    record = extraction_record(resources)
    assert grounded_counts(resources) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS


def test_provider_runs_after_preflight_run_lock_is_released(resources: ExtractionResources) -> None:
    provider = FakeProvider(response_for(resources))

    def acquire_run_lock(_: FactExtractionRequest) -> None:
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            assert RunRepository(connection).get_for_update(resources.run_id) is not None
            connection.commit()

    provider.before_return = acquire_run_lock
    result = service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    assert result.replayed is False
    assert provider.calls


def test_persistence_holds_same_run_lock_until_commit(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = threading.Event()
    release = threading.Event()
    original_create = extraction_module.EvidenceSpanRepository.create

    def pause_after_staging(repository, span):
        stored = original_create(repository, span)
        staged.set()
        assert release.wait(timeout=5)
        return stored

    monkeypatch.setattr(extraction_module.EvidenceSpanRepository, "create", pause_after_staging)
    extraction_service = service(FakeProvider(response_for(resources)))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(extraction_service.extract, ExtractRunFactsCommand(resources.run_id))
        assert staged.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                RunRepository(connection).get_for_update(resources.run_id)
            connection.rollback()
        release.set()
        future.result(timeout=5)


def test_two_callers_contend_for_fixed_idempotency_key_with_one_provider_call(
    resources: ExtractionResources,
) -> None:
    provider = FakeProvider(response_for(resources))
    provider_entered = threading.Event()
    release_provider = threading.Event()

    def pause_provider(_: FactExtractionRequest) -> None:
        provider_entered.set()
        assert release_provider.wait(timeout=5)

    provider.before_return = pause_provider
    extraction_service = service(provider)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(extraction_service.extract, ExtractRunFactsCommand(resources.run_id))
        assert provider_entered.wait(timeout=5)
        second = executor.submit(extraction_service.extract, ExtractRunFactsCommand(resources.run_id))
        with pytest.raises(FactExtractionInProgressError):
            second.result(timeout=5)
        release_provider.set()
        first.result(timeout=5)

    assert len(provider.calls) == 1
    assert grounded_counts(resources) == (1, 1, 1, 1)


@pytest.mark.parametrize("result_reference", [None, "fact-extraction:wrong:sha256:" + "0" * 64])
def test_completed_malformed_or_different_result_reference_conflicts_without_provider_call(
    resources: ExtractionResources,
    result_reference: str | None,
) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO idempotency_records (
                scope, key, status, result_reference, claimed_at, completed_at
            )
            VALUES (%s, %s, 'COMPLETED'::idempotency_status, %s, clock_timestamp(), now())
            """,
            ("fact-extraction-v1", f"understand:{resources.run_id}", result_reference),
        )
        connection.commit()

    provider = FakeProvider(response_for(resources))
    with pytest.raises(FactExtractionInputConflictError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    assert provider.calls == []
    assert grounded_counts(resources) == (0, 0, 0, 0)


def test_reclaimed_stale_token_rolls_back_all_staged_grounded_rows(
    resources: ExtractionResources,
) -> None:
    provider = FakeProvider(response_for(resources))

    def reclaim_claim(_: FactExtractionRequest) -> None:
        with pool.connection() as connection:
            repository = IdempotencyRepository(connection)
            record = repository.get("fact-extraction-v1", f"understand:{resources.run_id}")
            assert record is not None
            assert record.claimed_at is not None
            assert repository.reclaim(
                "fact-extraction-v1",
                f"understand:{resources.run_id}",
                record.claimed_at,
            ) is not None
            connection.commit()

    provider.before_return = reclaim_claim
    with pytest.raises(FactExtractionClaimLostError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    record = extraction_record(resources)
    assert grounded_counts(resources) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_retryable_persistence_failure_after_staging_retries_without_provider_recall(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    attempts = 0
    original_mark_completed = extraction_module.IdempotencyRepository.mark_completed

    def fail_once_after_staging(repository, *args):
        nonlocal attempts
        attempts += 1
        completed = original_mark_completed(repository, *args)
        if attempts == 1:
            raise retryable_error("retry persistence transaction")
        return completed

    monkeypatch.setattr(
        extraction_module.IdempotencyRepository,
        "mark_completed",
        fail_once_after_staging,
    )
    provider = FakeProvider(response_for(resources))

    result = service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    assert result.replayed is False
    assert attempts == 2
    assert len(provider.calls) == 1
    assert grounded_counts(resources) == (1, 1, 1, 1)
    record = extraction_record(resources)
    assert record is not None
    assert record.status is IdempotencyStatus.COMPLETED


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_three_retryable_persistence_failures_reraise_without_partial_rows(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    attempts = 0
    original_mark_completed = extraction_module.IdempotencyRepository.mark_completed

    def fail_every_staged_attempt(repository, *args):
        nonlocal attempts
        attempts += 1
        original_mark_completed(repository, *args)
        raise retryable_error("retry persistence transaction")

    monkeypatch.setattr(
        extraction_module.IdempotencyRepository,
        "mark_completed",
        fail_every_staged_attempt,
    )
    provider = FakeProvider(response_for(resources))

    with pytest.raises(retryable_error):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    assert attempts == 3
    assert len(provider.calls) == 1
    assert grounded_counts(resources) == (0, 0, 0, 0)
    record = extraction_record(resources)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS


def test_chunk_for_share_blocks_update_until_persistence_releases_lock(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = threading.Event()
    release = threading.Event()
    original_create = extraction_module.EvidenceSpanRepository.create

    def pause_after_chunk_lock(repository, span):
        stored = original_create(repository, span)
        staged.set()
        assert release.wait(timeout=5)
        return stored

    monkeypatch.setattr(extraction_module.EvidenceSpanRepository, "create", pause_after_chunk_lock)
    extraction_service = service(FakeProvider(response_for(resources)))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(extraction_service.extract, ExtractRunFactsCommand(resources.run_id))
        assert staged.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                connection.execute(
                    "UPDATE document_chunks SET content = %s WHERE id = %s::uuid",
                    ("blocked update", resources.chunk_id),
                )
            connection.rollback()
        release.set()
        future.result(timeout=5)

    with pool.connection() as connection:
        connection.execute(
            "UPDATE document_chunks SET content = %s WHERE id = %s::uuid",
            ("updated after release", resources.chunk_id),
        )
        connection.commit()
