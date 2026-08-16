import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
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
    InvalidFactExtractionResponseError,
    LegacyUpdateFactExtractionConflictError,
    SourceEvidenceValidationError,
    SourceInputDriftError,
    UpdateFactExtractionPreparationError,
)
from api.application.prepare_update_run_service import (
    PrepareUpdateRunCommand,
    PrepareUpdateRunService,
)
from db.pool import pool
from domain.fact_extraction import (
    FactEvidenceCandidate,
    FactExtractionCandidate,
    FactExtractionRequest,
    FactExtractionResponse,
    fact_extraction_input_fingerprint,
)
from repositories.idempotency_repository import (
    IdempotencyFailureKind,
    IdempotencyRepository,
    IdempotencyStatus,
)
from repositories.run_repository import RunRepository
from services.fact_extraction_provider import FactExtractionProviderError
from services.openai_structured_transport import StructuredTransportInvalidResponse
from services.openai_task_providers import OpenAIFactExtractionProvider


@dataclass(frozen=True)
class ExtractionResources:
    pile_id: str
    run_id: str
    document_version_id: str
    chunk_id: str
    other_document_version_id: str
    other_chunk_id: str


@dataclass(frozen=True)
class UpdateExtractionResources:
    pile_id: str
    baseline_run_id: str
    update_run_id: str
    retained_version_id: str
    retained_chunk_id: str
    trigger_version_id: str
    trigger_chunk_id: str


@dataclass(frozen=True)
class CarriedUpdateExtractionResources:
    pile_id: str
    baseline_run_id: str
    update_run_id: str
    older_origin_run_id: str
    retained_version_id: str
    retained_chunk_id: str
    replaced_version_id: str
    replaced_chunk_id: str
    trigger_version_id: str
    trigger_chunk_id: str
    carried_fact_id: str
    replaced_fact_id: str
    mixed_fact_id: str
    retained_evidence_id: str
    replaced_evidence_id: str


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


class InvalidStructuredTransport:
    def parse(self, **_: object):
        raise StructuredTransportInvalidResponse("invalid structured output")


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


@pytest.fixture
def update_resources() -> UpdateExtractionResources:
    with pool.connection() as connection:
        pile_id = create_pile(connection)
        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, completed_at)
            VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now())
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None
        retained_version_id, retained_chunk_id = create_document_version_with_chunk(
            connection, pile_id, "retained", "Retained source.",
        )
        replaced_version_id, _ = create_document_version_with_chunk(
            connection, pile_id, "changed", "Original source.",
        )
        connection.execute(
            """
            UPDATE document_versions
            SET processing_status = 'READY'::document_version_status
            WHERE id IN (%s::uuid, %s::uuid)
            """,
            (retained_version_id, replaced_version_id),
        )
        document = connection.execute(
            "SELECT document_id FROM document_versions WHERE id = %s::uuid",
            (replaced_version_id,),
        ).fetchone()
        assert document is not None
        trigger = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            )
            VALUES (%s::uuid, 2, %s, %s, 'READY')
            RETURNING id
            """,
            (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        assert trigger is not None
        trigger_version_id = str(trigger[0])
        trigger_chunk = connection.execute(
            """
            INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash, page_number)
            VALUES (%s::uuid, 0, %s, %s, 7)
            RETURNING id
            """,
            (trigger_version_id, "Changed amount is 101 USD.", f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert trigger_chunk is not None
        update = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, baseline_run_id, trigger_document_version_id
            )
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(baseline[0]), trigger_version_id),
        ).fetchone()
        assert update is not None
        update_run_id = str(update[0])
        connection.execute(
            """
            INSERT INTO run_document_versions (run_id, document_version_id)
            VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)
            """,
            (str(baseline[0]), retained_version_id, str(baseline[0]), replaced_version_id),
        )
        connection.commit()

    created = UpdateExtractionResources(
        pile_id,
        str(baseline[0]),
        update_run_id,
        retained_version_id,
        retained_chunk_id,
        trigger_version_id,
        str(trigger_chunk[0]),
    )
    PrepareUpdateRunService(pool).prepare(PrepareUpdateRunCommand(created.update_run_id))
    with pool.connection() as connection:
        connection.execute(
            "UPDATE runs SET status = 'RUNNING'::run_status WHERE id = %s::uuid",
            (created.update_run_id,),
        )
        connection.commit()
    try:
        yield created
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE key IN (%s, %s, %s)",
                (
                    f"understand-update:{created.update_run_id}",
                    f"understand:{created.update_run_id}",
                    f"prepare-update:{created.update_run_id}",
                ),
            )
            connection.execute(
                "DELETE FROM fact_evidence WHERE fact_id IN (SELECT id FROM facts WHERE origin_run_id = %s::uuid)",
                (created.update_run_id,),
            )
            connection.execute("DELETE FROM run_facts WHERE run_id = %s::uuid", (created.update_run_id,))
            connection.execute("DELETE FROM facts WHERE origin_run_id = %s::uuid", (created.update_run_id,))
            connection.execute(
                "DELETE FROM evidence_spans WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))",
                (created.pile_id,),
            )
            connection.execute("DELETE FROM run_document_versions WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (created.pile_id,))
            connection.execute("DELETE FROM document_chunks WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))", (created.pile_id,))
            connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute("DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid)", (created.pile_id,))
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (created.pile_id,))
            connection.commit()


@pytest.fixture
def carried_update_resources() -> CarriedUpdateExtractionResources:
    with pool.connection() as connection:
        pile_id = create_pile(connection)
        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, completed_at)
            VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now())
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        older_origin = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'MANUAL', 'PENDING')
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None and older_origin is not None
        retained_version_id, retained_chunk_id = create_document_version_with_chunk(
            connection, pile_id, f"retained-{uuid4()}", "Retained source.",
        )
        replaced_version_id, replaced_chunk_id = create_document_version_with_chunk(
            connection, pile_id, f"replaced-{uuid4()}", "Replaced source.",
        )
        connection.execute(
            """
            UPDATE document_versions
            SET processing_status = 'READY'::document_version_status
            WHERE id IN (%s::uuid, %s::uuid)
            """,
            (retained_version_id, replaced_version_id),
        )
        document = connection.execute(
            "SELECT document_id FROM document_versions WHERE id = %s::uuid",
            (replaced_version_id,),
        ).fetchone()
        assert document is not None
        trigger = connection.execute(
            """
            INSERT INTO document_versions (
                document_id, version_number, content_hash, storage_uri, processing_status
            )
            VALUES (%s::uuid, 2, %s, %s, 'READY')
            RETURNING id
            """,
            (str(document[0]), f"sha256:{uuid4().hex}", f"storage://{uuid4()}"),
        ).fetchone()
        assert trigger is not None
        trigger_version_id = str(trigger[0])
        trigger_chunk = connection.execute(
            """
            INSERT INTO document_chunks (document_version_id, chunk_index, content, content_hash)
            VALUES (%s::uuid, 0, %s, %s)
            RETURNING id
            """,
            (trigger_version_id, "Trigger value is 101.", f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert trigger_chunk is not None
        retained_evidence = connection.execute(
            """
            INSERT INTO evidence_spans (
                document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash
            )
            VALUES (%s::uuid, %s::uuid, 0, 8, 'Retained', %s)
            RETURNING id
            """,
            (retained_version_id, retained_chunk_id, f"sha256:{uuid4().hex}"),
        ).fetchone()
        replaced_evidence = connection.execute(
            """
            INSERT INTO evidence_spans (
                document_version_id, chunk_id, start_offset, end_offset, quoted_text, content_hash
            )
            VALUES (%s::uuid, %s::uuid, 0, 8, 'Replaced', %s)
            RETURNING id
            """,
            (replaced_version_id, replaced_chunk_id, f"sha256:{uuid4().hex}"),
        ).fetchone()
        assert retained_evidence is not None and replaced_evidence is not None

        def add_baseline_fact(subject: str, evidence_ids: tuple[str, ...]) -> str:
            fact = connection.execute(
                """
                INSERT INTO facts (origin_run_id, subject, predicate, object_value, status)
                VALUES (%s::uuid, %s, 'equals', '1'::jsonb, 'CONFIRMED'::fact_status)
                RETURNING id
                """,
                (str(older_origin[0]), subject),
            ).fetchone()
            assert fact is not None
            fact_id = str(fact[0])
            connection.execute(
                """
                INSERT INTO run_facts (run_id, fact_id, relation)
                VALUES (%s::uuid, %s::uuid, 'CARRIED_FORWARD'::run_membership_relation)
                """,
                (str(baseline[0]), fact_id),
            )
            for evidence_id in evidence_ids:
                connection.execute(
                    "INSERT INTO fact_evidence (fact_id, evidence_span_id) VALUES (%s::uuid, %s::uuid)",
                    (fact_id, evidence_id),
                )
            return fact_id

        carried_fact_id = add_baseline_fact("retained fact", (str(retained_evidence[0]),))
        replaced_fact_id = add_baseline_fact("replaced fact", (str(replaced_evidence[0]),))
        mixed_fact_id = add_baseline_fact(
            "mixed fact",
            (str(retained_evidence[0]), str(replaced_evidence[0])),
        )
        update = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, baseline_run_id, trigger_document_version_id
            )
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(baseline[0]), trigger_version_id),
        ).fetchone()
        assert update is not None
        update_run_id = str(update[0])
        connection.execute(
            """
            INSERT INTO run_document_versions (run_id, document_version_id)
            VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)
            """,
            (str(baseline[0]), retained_version_id, str(baseline[0]), replaced_version_id),
        )
        connection.commit()

    created = CarriedUpdateExtractionResources(
        pile_id, str(baseline[0]), update_run_id, str(older_origin[0]), retained_version_id,
        retained_chunk_id, replaced_version_id, replaced_chunk_id, trigger_version_id,
        str(trigger_chunk[0]), carried_fact_id, replaced_fact_id, mixed_fact_id,
        str(retained_evidence[0]), str(replaced_evidence[0]),
    )
    PrepareUpdateRunService(pool).prepare(PrepareUpdateRunCommand(created.update_run_id))
    with pool.connection() as connection:
        connection.execute(
            "UPDATE runs SET status = 'RUNNING'::run_status WHERE id = %s::uuid",
            (created.update_run_id,),
        )
        connection.commit()
    try:
        yield created
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE key LIKE %s",
                (f"%{created.update_run_id}%",),
            )
            connection.execute(
                "DELETE FROM fact_evidence WHERE fact_id IN (SELECT id FROM facts WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid))",
                (created.pile_id,),
            )
            connection.execute("DELETE FROM run_facts WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (created.pile_id,))
            connection.execute("DELETE FROM facts WHERE origin_run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (created.pile_id,))
            connection.execute("DELETE FROM evidence_spans WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))", (created.pile_id,))
            connection.execute("DELETE FROM run_document_versions WHERE run_id IN (SELECT id FROM runs WHERE pile_id = %s::uuid)", (created.pile_id,))
            connection.execute("DELETE FROM document_chunks WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid))", (created.pile_id,))
            connection.execute("DELETE FROM runs WHERE pile_id = %s::uuid", (created.pile_id,))
            connection.execute("DELETE FROM document_versions WHERE document_id IN (SELECT id FROM documents WHERE pile_id = %s::uuid)", (created.pile_id,))
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


def update_response_for(resources: UpdateExtractionResources) -> FactExtractionResponse:
    return FactExtractionResponse(
        format="fact-extraction-v1",
        candidates=(
            FactExtractionCandidate(
                subject="changed amount",
                predicate="equals",
                object_value=101,
                evidence=(
                    FactEvidenceCandidate(
                        chunk_id=resources.trigger_chunk_id,
                        relative_start_offset=18,
                        relative_end_offset=25,
                        quoted_text="101 USD",
                    ),
                ),
            ),
        ),
    )


def update_extraction_record(resources: UpdateExtractionResources):
    with pool.connection() as connection:
        return IdempotencyRepository(connection).get(
            "fact-extraction-update-v2",
            f"understand-update:{resources.update_run_id}",
        )


def update_grounded_counts(run_id: str, trigger_version_id: str) -> tuple[int, int, int, int]:
    with pool.connection() as connection:
        facts = connection.execute(
            "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
            (run_id,),
        ).fetchone()
        created = connection.execute(
            "SELECT count(*) FROM run_facts WHERE run_id = %s::uuid AND relation = 'CREATED'::run_membership_relation",
            (run_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT count(*) FROM evidence_spans WHERE document_version_id = %s::uuid",
            (trigger_version_id,),
        ).fetchone()
        links = connection.execute(
            """
            SELECT count(*) FROM fact_evidence
            JOIN facts ON facts.id = fact_evidence.fact_id
            WHERE facts.origin_run_id = %s::uuid
            """,
            (run_id,),
        ).fetchone()
    assert facts is not None and created is not None and evidence is not None and links is not None
    return int(facts[0]), int(created[0]), int(evidence[0]), int(links[0])


def carried_fact_ids(resources: CarriedUpdateExtractionResources) -> list[str]:
    with pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT fact_id FROM run_facts
            WHERE run_id = %s::uuid AND relation = 'CARRIED_FORWARD'::run_membership_relation
            ORDER BY fact_id
            """,
            (resources.update_run_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def carried_snapshot(resources: CarriedUpdateExtractionResources) -> tuple[object, ...]:
    with pool.connection() as connection:
        fact = connection.execute(
            """
            SELECT origin_run_id, subject, predicate, object_value, status
            FROM facts WHERE id = %s::uuid
            """,
            (resources.carried_fact_id,),
        ).fetchone()
        links = connection.execute(
            """
            SELECT evidence_span_id FROM fact_evidence
            WHERE fact_id = %s::uuid ORDER BY evidence_span_id
            """,
            (resources.carried_fact_id,),
        ).fetchall()
    assert fact is not None
    return (*fact, tuple(str(row[0]) for row in links))


def test_update_replaced_extraction_exposes_trigger_only_and_replays_after_progression(
    update_resources: UpdateExtractionResources,
) -> None:
    provider = FakeProvider(update_response_for(update_resources))
    extractor = service(provider)

    first = extractor.extract(ExtractRunFactsCommand(update_resources.update_run_id.upper()))

    assert first.replayed is False
    assert len(provider.calls) == 1
    request = provider.calls[0]
    assert request.format == "fact-extraction-update-v2"
    assert request.document_version_ids == (update_resources.trigger_version_id,)
    assert [chunk.chunk_id for chunk in request.source_chunks] == [update_resources.trigger_chunk_id]
    assert update_resources.retained_chunk_id not in [chunk.chunk_id for chunk in request.source_chunks]
    record = update_extraction_record(update_resources)
    assert record is not None
    assert record.status is IdempotencyStatus.COMPLETED
    assert record.result_reference == (
        f"fact-extraction-update:{update_resources.update_run_id}:{first.input_fingerprint}"
    )
    with pool.connection() as connection:
        membership = connection.execute(
            "SELECT relation FROM run_facts WHERE run_id = %s::uuid ORDER BY created_at DESC LIMIT 1",
            (update_resources.update_run_id,),
        ).fetchone()
        evidence = connection.execute(
            "SELECT document_version_id FROM evidence_spans WHERE chunk_id = %s::uuid",
            (update_resources.trigger_chunk_id,),
        ).fetchone()
        connection.execute(
            "UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid",
            (update_resources.update_run_id,),
        )
        connection.commit()
    assert membership == ("CREATED",)
    assert evidence is not None
    assert str(evidence[0]) == update_resources.trigger_version_id
    with pool.connection() as connection:
        legacy = IdempotencyRepository(connection).claim(
            "fact-extraction-v1",
            f"understand:{update_resources.update_run_id}",
        )
        assert legacy is not None
        connection.commit()

    replay = extractor.extract(ExtractRunFactsCommand(update_resources.update_run_id))

    assert replay.replayed is True
    assert replay.input_fingerprint == first.input_fingerprint
    assert len(provider.calls) == 1


def test_update_preparation_scope_drift_fails_before_provider(
    update_resources: UpdateExtractionResources,
) -> None:
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM run_document_versions WHERE run_id = %s::uuid AND document_version_id = %s::uuid",
            (update_resources.update_run_id, update_resources.retained_version_id),
        )
        connection.commit()
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    with pytest.raises(extraction_module.UpdateFactExtractionPreparationError):
        service(provider).extract(ExtractRunFactsCommand(update_resources.update_run_id))

    assert provider.calls == []
    assert update_extraction_record(update_resources) is None


def test_update_added_trigger_exposes_only_added_version_and_zero_fact_completes(
    update_resources: UpdateExtractionResources,
) -> None:
    with pool.connection() as connection:
        version_id, chunk_id = create_document_version_with_chunk(
            connection,
            update_resources.pile_id,
            "added",
            "Added source is 404.",
        )
        connection.execute(
            "UPDATE document_versions SET processing_status = 'READY'::document_version_status WHERE id = %s::uuid",
            (version_id,),
        )
        run = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, baseline_run_id, trigger_document_version_id
            )
            VALUES (%s::uuid, 'UPDATE', 'PENDING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (update_resources.pile_id, update_resources.baseline_run_id, version_id),
        ).fetchone()
        assert run is not None
        added_run_id = str(run[0])
        connection.commit()
    try:
        PrepareUpdateRunService(pool).prepare(PrepareUpdateRunCommand(added_run_id))
        with pool.connection() as connection:
            connection.execute(
                "UPDATE runs SET status = 'RUNNING'::run_status WHERE id = %s::uuid",
                (added_run_id,),
            )
            connection.commit()
        provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

        result = service(provider).extract(ExtractRunFactsCommand(added_run_id))

        assert result.replayed is False
        assert len(provider.calls) == 1
        assert provider.calls[0].document_version_ids == (version_id,)
        assert [chunk.chunk_id for chunk in provider.calls[0].source_chunks] == [chunk_id]
        assert update_resources.retained_chunk_id not in [
            chunk.chunk_id for chunk in provider.calls[0].source_chunks
        ]
        with pool.connection() as connection:
            facts = connection.execute(
                "SELECT count(*) FROM facts WHERE origin_run_id = %s::uuid",
                (added_run_id,),
            ).fetchone()
            record = IdempotencyRepository(connection).get(
                "fact-extraction-update-v2",
                f"understand-update:{added_run_id}",
            )
        assert facts == (0,)
        assert record is not None
        assert record.status is IdempotencyStatus.COMPLETED
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE key IN (%s, %s)",
                (f"understand-update:{added_run_id}", f"prepare-update:{added_run_id}"),
            )
            connection.execute("DELETE FROM run_document_versions WHERE run_id = %s::uuid", (added_run_id,))
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (added_run_id,))
            connection.execute("DELETE FROM document_chunks WHERE document_version_id = %s::uuid", (version_id,))
            connection.execute("DELETE FROM document_versions WHERE id = %s::uuid", (version_id,))
            connection.execute("DELETE FROM documents WHERE logical_key = %s", ("added",))
            connection.commit()


def test_update_legacy_v1_operation_conflicts_before_provider(
    update_resources: UpdateExtractionResources,
) -> None:
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "fact-extraction-v1",
            f"understand:{update_resources.update_run_id}",
        )
        assert claimed is not None
        connection.commit()
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    with pytest.raises(LegacyUpdateFactExtractionConflictError):
        service(provider).extract(ExtractRunFactsCommand(update_resources.update_run_id))

    assert provider.calls == []
    assert update_extraction_record(update_resources) is None


def test_unprepared_historical_update_legacy_v1_operation_conflicts_before_phase4a_validation() -> None:
    with pool.connection() as connection:
        pile_id = create_pile(connection)
        baseline = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status, completed_at)
            VALUES (%s::uuid, 'MANUAL', 'COMPLETED', now())
            RETURNING id
            """,
            (pile_id,),
        ).fetchone()
        assert baseline is not None
        trigger_version_id, trigger_chunk_id = create_document_version_with_chunk(
            connection,
            pile_id,
            f"legacy-trigger-{uuid4()}",
            "Historical trigger source.",
        )
        connection.execute(
            "UPDATE document_versions SET processing_status = 'READY'::document_version_status WHERE id = %s::uuid",
            (trigger_version_id,),
        )
        update = connection.execute(
            """
            INSERT INTO runs (
                pile_id, run_type, status, baseline_run_id, trigger_document_version_id
            )
            VALUES (%s::uuid, 'UPDATE', 'RUNNING', %s::uuid, %s::uuid)
            RETURNING id
            """,
            (pile_id, str(baseline[0]), trigger_version_id),
        ).fetchone()
        assert update is not None
        update_run_id = str(update[0])
        legacy = IdempotencyRepository(connection).claim(
            "fact-extraction-v1",
            f"understand:{update_run_id}",
        )
        assert legacy is not None
        connection.commit()
    try:
        provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

        with pytest.raises(LegacyUpdateFactExtractionConflictError):
            service(provider).extract(ExtractRunFactsCommand(update_run_id))

        assert provider.calls == []
        assert update_extraction_record_for_run(update_run_id) is None
        assert update_grounded_counts(update_run_id, trigger_version_id) == (0, 0, 0, 0)
    finally:
        with pool.connection() as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE key IN (%s, %s)",
                (f"understand:{update_run_id}", f"understand-update:{update_run_id}"),
            )
            connection.execute("DELETE FROM runs WHERE id IN (%s::uuid, %s::uuid)", (update_run_id, str(baseline[0])))
            connection.execute("DELETE FROM document_chunks WHERE id = %s::uuid", (trigger_chunk_id,))
            connection.execute("DELETE FROM document_versions WHERE id = %s::uuid", (trigger_version_id,))
            connection.execute("DELETE FROM documents WHERE pile_id = %s::uuid", (pile_id,))
            connection.execute("DELETE FROM piles WHERE id = %s::uuid", (pile_id,))
            connection.commit()


@pytest.mark.parametrize(
    ("result_reference", "error_type"),
    [
        ("not-a-v2-reference", extraction_module.FactExtractionIdempotencyIntegrityError),
        ("different", FactExtractionInputConflictError),
    ],
)
def test_update_completed_reference_must_match_before_provider(
    update_resources: UpdateExtractionResources,
    result_reference: str,
    error_type: type[Exception],
) -> None:
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))
    extractor = service(provider)
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "fact-extraction-update-v2",
            f"understand-update:{update_resources.update_run_id}",
        )
        assert claimed is not None
        if result_reference == "different":
            result_reference = (
                f"fact-extraction-update:{update_resources.update_run_id}:sha256:" + "f" * 64
            )
        connection.execute(
            """
            UPDATE idempotency_records
            SET status = 'COMPLETED'::idempotency_status, result_reference = %s, completed_at = now()
            WHERE scope = 'fact-extraction-update-v2' AND key = %s
            """,
            (result_reference, f"understand-update:{update_resources.update_run_id}"),
        )
        connection.commit()

    with pytest.raises(error_type):
        extractor.extract(ExtractRunFactsCommand(update_resources.update_run_id))

    assert provider.calls == []


def test_update_fresh_created_membership_conflicts_before_provider(
    update_resources: UpdateExtractionResources,
) -> None:
    with pool.connection() as connection:
        fact = connection.execute(
            """
            INSERT INTO facts (origin_run_id, subject, predicate, object_value, status)
            VALUES (%s::uuid, 'unexpected', 'equals', '1'::jsonb, 'CONFIRMED'::fact_status)
            RETURNING id
            """,
            (update_resources.update_run_id,),
        ).fetchone()
        assert fact is not None
        connection.execute(
            """
            INSERT INTO run_facts (run_id, fact_id, relation)
            VALUES (%s::uuid, %s::uuid, 'CREATED'::run_membership_relation)
            """,
            (update_resources.update_run_id, str(fact[0])),
        )
        connection.commit()
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    with pytest.raises(extraction_module.UpdateFactExtractionTargetStateConflictError):
        service(provider).extract(ExtractRunFactsCommand(update_resources.update_run_id))

    assert provider.calls == []
    assert update_extraction_record(update_resources) is None


def test_update_carries_only_retained_fact_and_preserves_historical_provenance(
    carried_update_resources: CarriedUpdateExtractionResources,
) -> None:
    resources = carried_update_resources
    before = carried_snapshot(resources)

    assert carried_fact_ids(resources) == [resources.carried_fact_id]
    assert resources.replaced_fact_id not in carried_fact_ids(resources)
    assert resources.mixed_fact_id not in carried_fact_ids(resources)

    provider = FakeProvider(
        FactExtractionResponse(
            format="fact-extraction-v1",
            candidates=(
                FactExtractionCandidate(
                    subject="trigger fact",
                    predicate="equals",
                    object_value=101,
                    evidence=(
                        FactEvidenceCandidate(
                            chunk_id=resources.trigger_chunk_id,
                            relative_start_offset=17,
                            relative_end_offset=20,
                            quoted_text="101",
                        ),
                    ),
                ),
            ),
        ),
    )

    service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))

    after = carried_snapshot(resources)
    assert before == after
    assert str(after[0]) == resources.older_origin_run_id
    assert after[-1] == (resources.retained_evidence_id,)
    assert carried_fact_ids(resources) == [resources.carried_fact_id]
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (1, 1, 1, 1)


def test_update_carried_membership_drift_fails_before_claim_or_provider(
    carried_update_resources: CarriedUpdateExtractionResources,
) -> None:
    resources = carried_update_resources
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM run_facts WHERE run_id = %s::uuid AND fact_id = %s::uuid",
            (resources.update_run_id, resources.carried_fact_id),
        )
        connection.commit()
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    with pytest.raises(extraction_module.UpdateFactExtractionPreparationError):
        service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))

    assert provider.calls == []
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (0, 0, 0, 0)
    assert update_extraction_record_for_run(resources.update_run_id) is None


@pytest.mark.parametrize("state", ["missing", "in_progress", "null", "malformed", "different"])
def test_update_preparation_reference_must_be_completed_and_exact_before_provider(
    update_resources: UpdateExtractionResources,
    state: str,
) -> None:
    run_id = update_resources.update_run_id
    with pool.connection() as connection:
        if state == "missing":
            connection.execute(
                "DELETE FROM idempotency_records WHERE scope = 'update-preparation-v1' AND key = %s",
                (f"prepare-update:{run_id}",),
            )
        elif state == "in_progress":
            connection.execute(
                """
                UPDATE idempotency_records SET status = 'IN_PROGRESS'::idempotency_status,
                    result_reference = NULL, completed_at = NULL
                WHERE scope = 'update-preparation-v1' AND key = %s
                """,
                (f"prepare-update:{run_id}",),
            )
        elif state == "null":
            connection.execute(
                """
                UPDATE idempotency_records SET result_reference = NULL
                WHERE scope = 'update-preparation-v1' AND key = %s
                """,
                (f"prepare-update:{run_id}",),
            )
        elif state == "malformed":
            connection.execute(
                """
                UPDATE idempotency_records SET result_reference = 'update-preparation:bad'
                WHERE scope = 'update-preparation-v1' AND key = %s
                """,
                (f"prepare-update:{run_id}",),
            )
        else:
            connection.execute(
                """
                UPDATE idempotency_records
                SET result_reference = %s
                WHERE scope = 'update-preparation-v1' AND key = %s
                """,
                (f"update-preparation:{run_id}:sha256:" + "f" * 64, f"prepare-update:{run_id}"),
            )
        connection.commit()
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    with pytest.raises(extraction_module.UpdateFactExtractionPreparationError):
        service(provider).extract(ExtractRunFactsCommand(run_id))

    assert provider.calls == []
    assert update_extraction_record(update_resources) is None
    assert update_grounded_counts(run_id, update_resources.trigger_version_id) == (0, 0, 0, 0)


def test_update_non_trigger_evidence_is_rejected_and_marks_claim_failed(
    update_resources: UpdateExtractionResources,
) -> None:
    invalid = FactExtractionResponse(
        format="fact-extraction-v1",
        candidates=(
            FactExtractionCandidate(
                subject="invalid",
                predicate="equals",
                object_value=1,
                evidence=(
                    FactEvidenceCandidate(
                        chunk_id=update_resources.retained_chunk_id,
                        relative_start_offset=0,
                        relative_end_offset=8,
                        quoted_text="Retained",
                    ),
                ),
            ),
        ),
    )
    provider = FakeProvider(invalid)

    with pytest.raises(SourceEvidenceValidationError):
        service(provider).extract(ExtractRunFactsCommand(update_resources.update_run_id))

    record = update_extraction_record(update_resources)
    assert len(provider.calls) == 1
    assert update_grounded_counts(
        update_resources.update_run_id,
        update_resources.trigger_version_id,
    ) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.FAILED
    assert record.failure_kind is IdempotencyFailureKind.RETRYABLE_EVIDENCE_VALIDATION


def test_openai_structured_invalid_fact_output_is_retryable_invalid_response(
    resources: ExtractionResources,
) -> None:
    provider = OpenAIFactExtractionProvider(InvalidStructuredTransport(), "test-model")

    with pytest.raises(InvalidFactExtractionResponseError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))

    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1",
            f"understand:{resources.run_id}",
        )
    assert record is not None and record.status is IdempotencyStatus.FAILED
    assert record.failure_kind is IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE


def test_update_provider_failure_marks_v2_claim_failed_without_changing_carried_state(
    carried_update_resources: CarriedUpdateExtractionResources,
) -> None:
    resources = carried_update_resources
    provider = FailingProvider()

    with pytest.raises(FactExtractionProviderFailure):
        service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))

    record = update_extraction_record_for_run(resources.update_run_id)
    assert provider.calls == 1
    assert carried_fact_ids(resources) == [resources.carried_fact_id]
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.FAILED
    assert record.failure_kind is IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE


def update_extraction_record_for_run(run_id: str):
    with pool.connection() as connection:
        return IdempotencyRepository(connection).get(
            "fact-extraction-update-v2",
            f"understand-update:{run_id}",
        )


def test_update_reclaimed_claim_token_rolls_back_new_rows_and_preserves_carried_state(
    carried_update_resources: CarriedUpdateExtractionResources,
) -> None:
    resources = carried_update_resources
    carried_before = carried_snapshot(resources)
    carried_ids_before = carried_fact_ids(resources)
    stale_generation: list[int] = []
    response = FactExtractionResponse(
        format="fact-extraction-v1",
        candidates=(
            FactExtractionCandidate(
                subject="trigger fact",
                predicate="equals",
                object_value=101,
                evidence=(
                    FactEvidenceCandidate(
                        chunk_id=resources.trigger_chunk_id,
                        relative_start_offset=17,
                        relative_end_offset=20,
                        quoted_text="101",
                    ),
                ),
            ),
        ),
    )
    provider = FakeProvider(response)

    def reclaim_claim(_: FactExtractionRequest) -> None:
        with pool.connection() as connection:
            repository = IdempotencyRepository(connection)
            record = repository.get(
                "fact-extraction-update-v2",
                f"understand-update:{resources.update_run_id}",
            )
            assert record is not None and record.claimed_at is not None
            stale_generation.append(record.claim_generation)
            connection.execute("UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s", ("fact-extraction-update-v2", f"understand-update:{resources.update_run_id}"))
            replacement = repository.reclaim_stale(
                "fact-extraction-update-v2",
                f"understand-update:{resources.update_run_id}",
                record.claim_generation,
                timedelta(minutes=5),
            )
            assert replacement is not None
            assert replacement.claim_generation == record.claim_generation + 1
            connection.commit()

    provider.before_return = reclaim_claim
    with pytest.raises(FactExtractionClaimLostError):
        service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))

    record = update_extraction_record_for_run(resources.update_run_id)
    assert len(provider.calls) == 1
    assert stale_generation
    assert carried_snapshot(resources) == carried_before
    assert carried_fact_ids(resources) == carried_ids_before == [resources.carried_fact_id]
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS
    assert record.claim_generation == stale_generation[0] + 1

    with pool.connection() as connection:
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            ("fact-extraction-update-v2", f"understand-update:{resources.update_run_id}"),
        )
        connection.commit()

    recovery_provider = FakeProvider(response)
    recovered = service(recovery_provider).extract(ExtractRunFactsCommand(resources.update_run_id))
    assert recovered.replayed is False
    assert len(recovery_provider.calls) == 1

    final = update_extraction_record_for_run(resources.update_run_id)
    assert final is not None
    assert final.status is IdempotencyStatus.COMPLETED
    assert final.claim_generation > stale_generation[0]
    assert carried_snapshot(resources) == carried_before
    assert carried_fact_ids(resources) == carried_ids_before
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (1, 1, 1, 1)
    with pool.connection() as connection:
        created = connection.execute(
            "SELECT subject, predicate, object_value FROM facts WHERE origin_run_id = %s::uuid",
            (resources.update_run_id,),
        ).fetchall()
        memberships = connection.execute(
            "SELECT fact_id, relation FROM run_facts WHERE run_id = %s::uuid ORDER BY relation, fact_id",
            (resources.update_run_id,),
        ).fetchall()
        evidence = connection.execute(
            "SELECT count(*) FROM evidence_spans WHERE document_version_id = %s::uuid",
            (resources.trigger_version_id,),
        ).fetchone()
        links = connection.execute(
            "SELECT count(*) FROM fact_evidence JOIN facts ON facts.id = fact_evidence.fact_id WHERE facts.origin_run_id = %s::uuid",
            (resources.update_run_id,),
        ).fetchone()
    assert created == [("trigger fact", "equals", 101)]
    assert sum(1 for _, relation in memberships if relation == "CREATED") == 1
    assert sum(1 for _, relation in memberships if relation == "CARRIED_FORWARD") == 1
    assert evidence == (1,)
    assert links == (1,)


def test_fresh_update_owner_precedes_phase4a_reconstruction(
    carried_update_resources: CarriedUpdateExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = carried_update_resources
    scope = "fact-extraction-update-v2"
    key = f"understand-update:{resources.update_run_id}"
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.commit()
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    def invalid_request(*_args, **_kwargs):
        raise UpdateFactExtractionPreparationError("current preparation is invalid")

    monkeypatch.setattr(ExtractRunFactsService, "_load_update_request", invalid_request)
    with pytest.raises(FactExtractionInProgressError):
        service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))
    assert provider.calls == []
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, key)
        assert current is not None
        assert current.claim_generation == record.claim_generation
        assert current.claimed_at == record.claimed_at


def test_classification_change_to_completed_replays_without_provider(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "fact-extraction-v1"
    key = f"understand:{resources.run_id}"
    with pool.connection() as connection:
        observed = IdempotencyRepository(connection).claim(scope, key)
        assert observed is not None
        connection.commit()
    provider = FakeProvider(response_for(resources))
    owner = service(provider)
    original_classifier = extraction_module.classify_in_progress

    def complete_then_classify(repository, record, stale_after):
        with pool.connection() as other:
            request = owner._load_request(other, resources.run_id, for_share=False)
            fingerprint = fact_extraction_input_fingerprint(request)
            completed = IdempotencyRepository(other).mark_completed(
                scope,
                key,
                observed.claim_generation,
                owner._result_reference(resources.run_id, fingerprint),
            )
            assert completed is not None
            other.commit()
        state = original_classifier(repository, record, stale_after)
        assert state.value == "CHANGED_OR_MISSING"
        return state

    monkeypatch.setattr(extraction_module, "classify_in_progress", complete_then_classify)
    replay = owner.extract(ExtractRunFactsCommand(resources.run_id))
    assert replay.replayed
    assert provider.calls == []
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get(scope, key)
    assert durable is not None
    assert durable.status is IdempotencyStatus.COMPLETED
    assert durable.claim_generation == observed.claim_generation


def test_classification_change_to_new_generation_returns_in_progress(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "fact-extraction-v1"
    key = f"understand:{resources.run_id}"
    with pool.connection() as connection:
        observed = IdempotencyRepository(connection).claim(scope, key)
        assert observed is not None
        connection.commit()
    provider = FakeProvider(response_for(resources))
    original_classifier = extraction_module.classify_in_progress

    def reclaim_then_classify(repository, record, stale_after):
        with pool.connection() as other:
            other.execute(
                "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
                (scope, key),
            )
            replacement = IdempotencyRepository(other).reclaim_stale(
                scope, key, observed.claim_generation, timedelta(minutes=5),
            )
            assert replacement is not None
            other.commit()
        state = original_classifier(repository, record, stale_after)
        assert state.value == "CHANGED_OR_MISSING"
        return state

    monkeypatch.setattr(extraction_module, "classify_in_progress", reclaim_then_classify)
    with pytest.raises(FactExtractionInProgressError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))
    assert provider.calls == []
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get(scope, key)
    assert durable is not None
    assert durable.claim_generation == observed.claim_generation + 1


def test_lost_stale_reclaim_cas_rereads_new_owner_without_provider(
    resources: ExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = "fact-extraction-v1"
    key = f"understand:{resources.run_id}"
    with pool.connection() as connection:
        observed = IdempotencyRepository(connection).claim(scope, key)
        assert observed is not None
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, key),
        )
        connection.commit()
    provider = FakeProvider(response_for(resources))
    original_reclaim = extraction_module.reclaim_if_stale

    def competing_reclaim(repository, record, stale_after):
        with pool.connection() as other:
            winner = IdempotencyRepository(other).reclaim_stale(
                scope, key, observed.claim_generation, timedelta(minutes=5),
            )
            assert winner is not None
            other.commit()
        return original_reclaim(repository, record, stale_after)

    monkeypatch.setattr(extraction_module, "reclaim_if_stale", competing_reclaim)
    with pytest.raises(FactExtractionInProgressError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))
    assert provider.calls == []
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get(scope, key)
    assert durable is not None
    assert durable.claim_generation == observed.claim_generation + 1


def test_threaded_stale_owner_completion_loses_fence_and_rolls_back_rows(
    resources: ExtractionResources,
) -> None:
    a_started = threading.Event()
    release_a = threading.Event()

    def response(subject: str) -> FactExtractionResponse:
        return FactExtractionResponse(
            format="fact-extraction-v1",
            candidates=(
                FactExtractionCandidate(
                    subject=subject,
                    predicate="equals",
                    object_value=100,
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

    provider_a = FakeProvider(response("owner-a"))
    provider_b = FakeProvider(response("owner-b"))

    def pause_a(_: FactExtractionRequest) -> None:
        a_started.set()
        assert release_a.wait(timeout=10)

    provider_a.before_return = pause_a
    worker_a = service(provider_a)
    worker_b = service(provider_b)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future_a = executor.submit(worker_a.extract, ExtractRunFactsCommand(resources.run_id))
        assert a_started.wait(timeout=10)
        with pool.connection() as connection:
            record = IdempotencyRepository(connection).get(
                "fact-extraction-v1", f"understand:{resources.run_id}",
            )
            assert record is not None
            original_generation = record.claim_generation
            connection.execute(
                "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
                ("fact-extraction-v1", f"understand:{resources.run_id}"),
            )
            connection.commit()
        replacement = worker_b.extract(ExtractRunFactsCommand(resources.run_id))
        assert not replacement.replayed
        release_a.set()
        with pytest.raises(FactExtractionClaimLostError):
            future_a.result(timeout=10)

    with pool.connection() as connection:
        record = IdempotencyRepository(connection).get(
            "fact-extraction-v1", f"understand:{resources.run_id}",
        )
        rows = connection.execute(
            "SELECT subject FROM facts WHERE origin_run_id = %s::uuid ORDER BY subject",
            (resources.run_id,),
        ).fetchall()
        memberships = connection.execute(
            "SELECT count(*) FROM run_facts WHERE run_id = %s::uuid",
            (resources.run_id,),
        ).fetchone()
    assert record is not None
    assert record.status is IdempotencyStatus.COMPLETED
    assert record.claim_generation == original_generation + 1
    assert [row[0] for row in rows] == ["owner-b"]
    assert memberships == (1,)
    assert len(provider_a.calls) == 1
    assert len(provider_b.calls) == 1


def test_threaded_stale_owner_provider_failure_loses_failure_fence(
    resources: ExtractionResources,
) -> None:
    a_started = threading.Event()
    release_a = threading.Event()

    class BlockingFailureProvider:
        def __init__(self) -> None:
            self.calls = 0

        def extract(self, request: FactExtractionRequest) -> FactExtractionResponse:
            self.calls += 1
            a_started.set()
            assert release_a.wait(timeout=10)
            raise FactExtractionProviderError("old owner failed")

    provider_a = BlockingFailureProvider()
    provider_b = FakeProvider(response_for(resources))
    worker_a = service(provider_a)  # type: ignore[arg-type]
    worker_b = service(provider_b)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future_a = executor.submit(worker_a.extract, ExtractRunFactsCommand(resources.run_id))
        assert a_started.wait(timeout=10)
        with pool.connection() as connection:
            record = IdempotencyRepository(connection).get("fact-extraction-v1", f"understand:{resources.run_id}")
            assert record is not None
            original_generation = record.claim_generation
            connection.execute(
                "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
                ("fact-extraction-v1", f"understand:{resources.run_id}"),
            )
            connection.commit()
        worker_b.extract(ExtractRunFactsCommand(resources.run_id))
        release_a.set()
        with pytest.raises(FactExtractionClaimLostError):
            future_a.result(timeout=10)
    with pool.connection() as connection:
        durable = IdempotencyRepository(connection).get("fact-extraction-v1", f"understand:{resources.run_id}")
    assert durable is not None
    assert durable.status is IdempotencyStatus.COMPLETED
    assert durable.claim_generation == original_generation + 1
    assert provider_a.calls == 1
    assert len(provider_b.calls) == 1


def test_stale_v1_claim_with_ineligible_run_does_not_reclaim(
    resources: ExtractionResources,
) -> None:
    scope = "fact-extraction-v1"
    key = f"understand:{resources.run_id}"
    provider = FakeProvider(response_for(resources))
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, key),
        )
        connection.execute(
            "UPDATE runs SET status = 'REVIEW_REQUIRED'::run_status WHERE id = %s::uuid",
            (resources.run_id,),
        )
        connection.commit()
    with pool.connection() as connection:
        aged = IdempotencyRepository(connection).get(scope, key)
    assert aged is not None and aged.claimed_at is not None
    with pytest.raises(FactExtractionRunNotEligibleError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))
    assert provider.calls == []
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, key)
    assert current is not None
    assert current.status is IdempotencyStatus.IN_PROGRESS
    assert current.claim_generation == aged.claim_generation
    assert current.claimed_at == aged.claimed_at


def test_stale_update_v2_claim_with_prepared_scope_drift_does_not_reclaim(
    carried_update_resources: CarriedUpdateExtractionResources,
) -> None:
    resources = carried_update_resources
    scope = "fact-extraction-update-v2"
    key = f"understand-update:{resources.update_run_id}"
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))
    with pool.connection() as connection:
        record = IdempotencyRepository(connection).claim(scope, key)
        assert record is not None
        connection.execute(
            "UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s",
            (scope, key),
        )
        connection.execute(
            "DELETE FROM run_document_versions WHERE run_id = %s::uuid AND document_version_id = %s::uuid",
            (resources.update_run_id, resources.trigger_version_id),
        )
        connection.commit()
    with pool.connection() as connection:
        aged = IdempotencyRepository(connection).get(scope, key)
    assert aged is not None and aged.claimed_at is not None
    with pytest.raises(UpdateFactExtractionPreparationError):
        service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))
    assert provider.calls == []
    with pool.connection() as connection:
        current = IdempotencyRepository(connection).get(scope, key)
    assert current is not None
    assert current.status is IdempotencyStatus.IN_PROGRESS
    assert current.claim_generation == aged.claim_generation
    assert current.claimed_at == aged.claimed_at


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_update_retryable_persistence_failure_reuses_provider_and_preserves_carried_state(
    carried_update_resources: CarriedUpdateExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    resources = carried_update_resources
    attempts = 0
    original_mark_completed = extraction_module.IdempotencyRepository.mark_completed

    def fail_once_after_staging(repository, *args):
        nonlocal attempts
        attempts += 1
        completed = original_mark_completed(repository, *args)
        if attempts == 1:
            raise retryable_error("retry UPDATE persistence")
        return completed

    monkeypatch.setattr(extraction_module.IdempotencyRepository, "mark_completed", fail_once_after_staging)
    provider = FakeProvider(
        FactExtractionResponse(
            format="fact-extraction-v1",
            candidates=(
                FactExtractionCandidate(
                    subject="trigger fact",
                    predicate="equals",
                    object_value=101,
                    evidence=(
                        FactEvidenceCandidate(
                            chunk_id=resources.trigger_chunk_id,
                            relative_start_offset=17,
                            relative_end_offset=20,
                            quoted_text="101",
                        ),
                    ),
                ),
            ),
        ),
    )

    service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))

    record = update_extraction_record_for_run(resources.update_run_id)
    assert attempts == 2
    assert len(provider.calls) == 1
    assert carried_fact_ids(resources) == [resources.carried_fact_id]
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (1, 1, 1, 1)
    assert record is not None
    assert record.status is IdempotencyStatus.COMPLETED


@pytest.mark.parametrize("retryable_error", [DeadlockDetected, SerializationFailure])
def test_update_retry_exhaustion_rolls_back_new_rows_and_keeps_claim_recoverable(
    carried_update_resources: CarriedUpdateExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
    retryable_error: type[Exception],
) -> None:
    resources = carried_update_resources
    attempts = 0
    original_mark_completed = extraction_module.IdempotencyRepository.mark_completed

    def fail_every_staged_attempt(repository, *args):
        nonlocal attempts
        attempts += 1
        original_mark_completed(repository, *args)
        raise retryable_error("retry UPDATE persistence")

    monkeypatch.setattr(extraction_module.IdempotencyRepository, "mark_completed", fail_every_staged_attempt)
    provider = FakeProvider(
        FactExtractionResponse(
            format="fact-extraction-v1",
            candidates=(
                FactExtractionCandidate(
                    subject="trigger fact",
                    predicate="equals",
                    object_value=101,
                    evidence=(
                        FactEvidenceCandidate(
                            chunk_id=resources.trigger_chunk_id,
                            relative_start_offset=17,
                            relative_end_offset=20,
                            quoted_text="101",
                        ),
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(retryable_error):
        service(provider).extract(ExtractRunFactsCommand(resources.update_run_id))

    record = update_extraction_record_for_run(resources.update_run_id)
    assert attempts == 3
    assert len(provider.calls) == 1
    assert carried_fact_ids(resources) == [resources.carried_fact_id]
    assert update_grounded_counts(resources.update_run_id, resources.trigger_version_id) == (0, 0, 0, 0)
    assert record is not None
    assert record.status is IdempotencyStatus.IN_PROGRESS


def test_update_provider_runs_after_preflight_lock_release(
    update_resources: UpdateExtractionResources,
) -> None:
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))

    def acquire_run_lock(_: FactExtractionRequest) -> None:
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            assert RunRepository(connection).get_for_update(update_resources.update_run_id) is not None
            connection.commit()

    provider.before_return = acquire_run_lock
    service(provider).extract(ExtractRunFactsCommand(update_resources.update_run_id))

    assert len(provider.calls) == 1


def test_update_preflight_holds_run_lock_until_claim_commit(
    update_resources: UpdateExtractionResources,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = threading.Event()
    release = threading.Event()
    original_claim = extraction_module.IdempotencyRepository.claim

    def pause_claim(repository, scope, key):
        record = original_claim(repository, scope, key)
        if scope == "fact-extraction-update-v2":
            claimed.set()
            assert release.wait(timeout=5)
        return record

    monkeypatch.setattr(extraction_module.IdempotencyRepository, "claim", pause_claim)
    provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service(provider).extract, ExtractRunFactsCommand(update_resources.update_run_id))
        assert claimed.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                RunRepository(connection).get_for_update(update_resources.update_run_id)
            connection.rollback()
        release.set()
        future.result(timeout=5)


def test_update_persistence_holds_run_and_trigger_chunk_locks_until_commit(
    update_resources: UpdateExtractionResources,
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
    extraction_service = service(FakeProvider(update_response_for(update_resources)))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(extraction_service.extract, ExtractRunFactsCommand(update_resources.update_run_id))
        assert staged.wait(timeout=5)
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                RunRepository(connection).get_for_update(update_resources.update_run_id)
            connection.rollback()
        with pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            with pytest.raises(LockNotAvailable):
                connection.execute(
                    "UPDATE document_chunks SET content = 'blocked' WHERE id = %s::uuid",
                    (update_resources.trigger_chunk_id,),
                )
            connection.rollback()
        with pool.connection() as connection:
            connection.execute(
                "UPDATE document_chunks SET content = 'retained may change' WHERE id = %s::uuid",
                (update_resources.retained_chunk_id,),
            )
            connection.commit()
        release.set()
        future.result(timeout=5)


def test_initial_run_uses_v1_full_scope_without_update_v2_claim(resources: ExtractionResources) -> None:
    with pool.connection() as connection:
        row = connection.execute(
            """
            INSERT INTO runs (pile_id, run_type, status)
            VALUES (%s::uuid, 'INITIAL', 'RUNNING')
            RETURNING id
            """,
            (resources.pile_id,),
        ).fetchone()
        assert row is not None
        initial_run_id = str(row[0])
        connection.execute(
            """
            INSERT INTO run_document_versions (run_id, document_version_id)
            VALUES (%s::uuid, %s::uuid), (%s::uuid, %s::uuid)
            """,
            (initial_run_id, resources.document_version_id, initial_run_id, resources.other_document_version_id),
        )
        connection.commit()
    try:
        provider = FakeProvider(FactExtractionResponse(format="fact-extraction-v1", candidates=()))
        service(provider).extract(ExtractRunFactsCommand(initial_run_id))
        with pool.connection() as connection:
            v1 = IdempotencyRepository(connection).get("fact-extraction-v1", f"understand:{initial_run_id}")
            v2 = IdempotencyRepository(connection).get("fact-extraction-update-v2", f"understand-update:{initial_run_id}")
        assert len(provider.calls) == 1
        assert {chunk.document_version_id for chunk in provider.calls[0].source_chunks} == {
            resources.document_version_id,
            resources.other_document_version_id,
        }
        assert v1 is not None and v1.status is IdempotencyStatus.COMPLETED
        assert v2 is None
    finally:
        with pool.connection() as connection:
            connection.execute("DELETE FROM idempotency_records WHERE key = %s", (f"understand:{initial_run_id}",))
            connection.execute("DELETE FROM run_document_versions WHERE run_id = %s::uuid", (initial_run_id,))
            connection.execute("DELETE FROM runs WHERE id = %s::uuid", (initial_run_id,))
            connection.commit()


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


def test_normal_failed_retryable_extraction_never_reopens_without_workflow(
    resources: ExtractionResources,
) -> None:
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim("fact-extraction-v1", f"understand:{resources.run_id}")
        assert claimed is not None
        IdempotencyRepository(connection).mark_failed(
            claimed.scope, claimed.key, claimed.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        connection.commit()
    provider = FakeProvider(response_for(resources))
    with pytest.raises(extraction_module.FactExtractionPreviouslyFailedError):
        service(provider).extract(ExtractRunFactsCommand(resources.run_id))
    current = extraction_record(resources)
    assert provider.calls == []
    assert current is not None and current.status is IdempotencyStatus.FAILED
    assert current.claim_generation == claimed.claim_generation
    assert current.failure_kind is IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE


def test_concurrent_workflow_only_extraction_reopen_has_one_provider_winner(
    resources: ExtractionResources,
) -> None:
    with pool.connection() as connection:
        claimed = IdempotencyRepository(connection).claim(
            "fact-extraction-v1", f"understand:{resources.run_id}",
        )
        assert claimed is not None
        failed = IdempotencyRepository(connection).mark_failed(
            claimed.scope,
            claimed.key,
            claimed.claim_generation,
            IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE,
        )
        assert failed is not None
        connection.commit()
    entered = threading.Event()
    release = threading.Event()
    provider = FakeProvider(response_for(resources))

    def pause(_request: FactExtractionRequest) -> None:
        entered.set()
        assert release.wait(10)

    provider.before_return = pause
    owner = service(provider)
    loser = service(provider)
    with ThreadPoolExecutor(max_workers=1) as executor:
        winning = executor.submit(
            owner._retry_after_failed_from_workflow,
            ExtractRunFactsCommand(resources.run_id),
            expected_failed_generation=failed.claim_generation,
        )
        assert entered.wait(10)
        with pytest.raises(extraction_module.FactExtractionInProgressError):
            loser._retry_after_failed_from_workflow(
                ExtractRunFactsCommand(resources.run_id),
                expected_failed_generation=failed.claim_generation,
            )
        release.set()
        winning.result(timeout=10)
    current = extraction_record(resources)
    assert len(provider.calls) == 1
    assert current is not None and current.status is IdempotencyStatus.COMPLETED
    assert current.claim_generation == failed.claim_generation + 1


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
            connection.execute("UPDATE idempotency_records SET claimed_at = clock_timestamp() - interval '6 minutes' WHERE scope = %s AND key = %s", ("fact-extraction-v1", f"understand:{resources.run_id}"))
            assert repository.reclaim_stale("fact-extraction-v1", f"understand:{resources.run_id}", record.claim_generation, timedelta(minutes=5)) is not None
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
