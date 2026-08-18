from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from api.application.classify_document_version_service import (
    ClassifyDocumentVersionCommand,
    ClassifyDocumentVersionService,
    DocumentClassificationInvalidResponseError,
    DocumentClassificationProviderFailureError,
)
from config.settings import get_settings
from domain.document_classification import DocumentClassificationResponse, DocumentRole
from repositories.idempotency_repository import IdempotencyRepository, IdempotencyStatus
from services.document_classification_provider import DocumentClassificationProviderError
from services.openai_structured_transport import ProviderUsage

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = tuple(sorted((_PROJECT_ROOT / "sql" / "migrations").glob("*.sql")))


@dataclass(frozen=True)
class _ClassificationDatabase:
    pool: ConnectionPool
    conninfo: str
    document_version_id: str
    chunk_ids: tuple[str, str]


class _Provider:
    provider_name = "test-provider"
    model_name = "test-model"

    def __init__(self, responses: list[object]) -> None:
        self._responses = responses
        self.calls = []
        self.last_usage = ProviderUsage("test-provider", "test-model", 21, 7)

    def classify(self, request):
        self.calls.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def classification_database() -> _ClassificationDatabase:
    name = f"task1_phase6c1_classification_{uuid4().hex}"
    info = conninfo_to_dict(get_settings().database_url)
    info["dbname"] = "postgres"
    admin_conninfo = make_conninfo(**info)
    with connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')

    database_info = dict(info)
    database_info["dbname"] = name
    conninfo = make_conninfo(**database_info)
    with connect(conninfo) as connection:
        for migration in _MIGRATIONS:
            connection.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
        pile_id = str(
            connection.execute(
                "INSERT INTO piles (name) VALUES ('classification') RETURNING id",
            ).fetchone()[0],
        )
        document_id = str(
            connection.execute(
                "INSERT INTO documents (pile_id, logical_key) VALUES (%s::uuid, 'closing') RETURNING id",
                (pile_id,),
            ).fetchone()[0],
        )
        version_id = str(
            connection.execute(
                """
                INSERT INTO document_versions (
                    document_id, version_number, content_hash, storage_uri,
                    mime_type, size_bytes, processing_status
                )
                VALUES (
                    %s::uuid, 1, %s, %s, 'text/plain', 80,
                    'READY'::document_version_status
                )
                RETURNING id
                """,
                (document_id, "sha256:" + "a" * 64, "source://sha256/" + "a" * 64),
            ).fetchone()[0],
        )
        chunk_ids = []
        for index, content in enumerate(
            (
                "Closing Disclosure — Loan Terms and Projected Payments",
                "Borrower acknowledges receipt of this Closing Disclosure.",
            ),
        ):
            row = connection.execute(
                """
                INSERT INTO document_chunks (
                    document_version_id, chunk_index, content, content_hash,
                    page_number, start_offset, end_offset
                )
                VALUES (%s::uuid, %s, %s, %s, %s, 0, %s)
                RETURNING id
                """,
                (
                    version_id,
                    index,
                    content,
                    f"sha256:{index + 1:064x}",
                    index + 1,
                    len(content),
                ),
            ).fetchone()
            assert row is not None
            chunk_ids.append(str(row[0]))
        connection.execute(
            "UPDATE documents SET current_version_id = %s::uuid WHERE id = %s::uuid",
            (version_id, document_id),
        )
        connection.commit()

    pool = ConnectionPool(conninfo, min_size=1, max_size=3, open=False)
    pool.open()
    try:
        yield _ClassificationDatabase(pool, conninfo, version_id, (chunk_ids[0], chunk_ids[1]))
    finally:
        pool.close()
        with connect(admin_conninfo, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _closing_response(chunk_id: str) -> DocumentClassificationResponse:
    return DocumentClassificationResponse(
        format="document-classification-v1",
        role=DocumentRole.CLOSING_DISCLOSURE,
        confidence=0.99,
        evidence_chunk_ids=(chunk_id,),
        explanation="The supplied source explicitly identifies itself as a Closing Disclosure.",
    )


def test_classification_is_grounded_durable_and_exact_replay_is_provider_free(
    classification_database: _ClassificationDatabase,
) -> None:
    provider = _Provider([_closing_response(classification_database.chunk_ids[0])])
    service = ClassifyDocumentVersionService(classification_database.pool, provider)  # type: ignore[arg-type]

    first = service.classify(
        ClassifyDocumentVersionCommand(classification_database.document_version_id),
    )
    replay = service.classify(
        ClassifyDocumentVersionCommand(classification_database.document_version_id),
    )

    assert first.role is DocumentRole.CLOSING_DISCLOSURE
    assert first.evidence_chunk_ids == (classification_database.chunk_ids[0],)
    assert first.provider_name == "test-provider"
    assert first.model_name == "test-model"
    assert (first.input_tokens, first.output_tokens) == (21, 7)
    assert first.replayed is False
    assert replay == type(replay)(**{**first.__dict__, "replayed": True})
    assert len(provider.calls) == 1

    with connect(classification_database.conninfo) as connection:
        persisted = connection.execute(
            """
            SELECT role::text, provider_name, model_name, input_tokens, output_tokens
            FROM document_classifications
            WHERE document_version_id = %s::uuid
            """,
            (classification_database.document_version_id,),
        ).fetchone()
        claim = IdempotencyRepository(connection).get(
            "document-classification-v1",
            f"classify:{classification_database.document_version_id}",
        )
    assert persisted == ("CLOSING_DISCLOSURE", "test-provider", "test-model", 21, 7)
    assert claim is not None and claim.status is IdempotencyStatus.COMPLETED


def test_retryable_provider_failure_reopens_same_classification_identity(
    classification_database: _ClassificationDatabase,
) -> None:
    provider = _Provider(
        [
            DocumentClassificationProviderError("temporary provider outage"),
            _closing_response(classification_database.chunk_ids[0]),
        ],
    )
    service = ClassifyDocumentVersionService(classification_database.pool, provider)  # type: ignore[arg-type]
    command = ClassifyDocumentVersionCommand(classification_database.document_version_id)

    with pytest.raises(DocumentClassificationProviderFailureError):
        service.classify(command)
    completed = service.classify(command)

    assert completed.role is DocumentRole.CLOSING_DISCLOSURE
    assert len(provider.calls) == 2
    with connect(classification_database.conninfo) as connection:
        claim = IdempotencyRepository(connection).get(
            "document-classification-v1",
            f"classify:{classification_database.document_version_id}",
        )
    assert claim is not None
    assert claim.status is IdempotencyStatus.COMPLETED
    assert claim.claim_generation == 2


def test_classification_fails_closed_on_evidence_outside_supplied_chunks(
    classification_database: _ClassificationDatabase,
) -> None:
    provider = _Provider(
        [
            DocumentClassificationResponse(
                format="document-classification-v1",
                role=DocumentRole.CLOSING_DISCLOSURE,
                confidence=0.8,
                evidence_chunk_ids=("99999999-9999-4999-8999-999999999999",),
                explanation="Unsupported evidence identity.",
            ),
        ],
    )
    service = ClassifyDocumentVersionService(classification_database.pool, provider)  # type: ignore[arg-type]

    with pytest.raises(DocumentClassificationInvalidResponseError):
        service.classify(
            ClassifyDocumentVersionCommand(classification_database.document_version_id),
        )

    with connect(classification_database.conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM document_classifications").fetchone() == (0,)
        claim = IdempotencyRepository(connection).get(
            "document-classification-v1",
            f"classify:{classification_database.document_version_id}",
        )
    assert claim is not None and claim.status is IdempotencyStatus.FAILED


def test_unknown_role_is_allowed_without_invented_evidence(
    classification_database: _ClassificationDatabase,
) -> None:
    provider = _Provider(
        [
            DocumentClassificationResponse(
                format="document-classification-v1",
                role=DocumentRole.UNKNOWN,
                confidence=0.2,
                evidence_chunk_ids=(),
                explanation="The sampled source does not establish a supported mortgage document role.",
            ),
        ],
    )
    service = ClassifyDocumentVersionService(classification_database.pool, provider)  # type: ignore[arg-type]

    result = service.classify(
        ClassifyDocumentVersionCommand(classification_database.document_version_id),
    )

    assert result.role is DocumentRole.UNKNOWN
    assert result.evidence_chunk_ids == ()
