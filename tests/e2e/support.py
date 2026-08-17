from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from fastapi import FastAPI
from psycopg import connect
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from api.application.build_update_change_set_service import BuildUpdateChangeSetService
from api.application.commit_final_report_service import CommitFinalReportService
from api.application.create_pile_service import CreatePileService
from api.application.create_rule_set_service import CreateRuleSetService
from api.application.detect_run_contradictions_service import DetectRunContradictionsService
from api.application.document_arrival_service import DocumentArrivalService
from api.application.evaluate_run_rules_service import EvaluateRunRulesService
from api.application.external_advance_run_service import ExternalAdvanceRunService
from api.application.external_query_service import ExternalQueryService
from api.application.external_review_query_service import ExternalReviewQueryService
from api.application.external_update_review_service import ExternalUpdateReviewService
from api.application.extract_run_facts_service import ExtractRunFactsService
from api.application.finalize_run_service import FinalizeRunService
from api.application.ingest_and_reconcile_document_service import IngestAndReconcileDocumentService
from api.application.ingest_document_service import IngestDocumentService
from api.application.prepare_update_run_service import PrepareUpdateRunService
from api.application.review_decision_service import ReviewDecisionService
from api.application.review_update_change_set_service import ReviewUpdateChangeSetService
from api.application.run_rule_scope_service import RunRuleScopeService
from api.application.run_workflow_service import RunWorkflowService
from api.mcp.server import create_mcp_server
from api.mcp.service_provider import McpServiceProvider
from api.rest.dependencies import (
    get_create_pile_service,
    get_create_rule_set_service,
    get_external_advance_run_service,
    get_external_query_service,
    get_external_review_query_service,
    get_external_update_review_service,
    get_ingest_and_reconcile_document_service,
    get_review_decision_service,
)
from api.rest.errors import install_error_handlers
from api.rest.router import api_v1_router
from config.settings import get_settings
from domain.contradiction_detection import ContradictionDetectionResponse
from domain.fact_extraction import (
    FactEvidenceCandidate,
    FactExtractionCandidate,
    FactExtractionRequest,
    FactExtractionResponse,
    UpdateFactExtractionRequest,
)
from domain.rule_evaluation import (
    RuleEvaluationOutcome,
    RuleEvaluationRequest,
    RuleEvaluationResponse,
    RuleOutcomeType,
    RuleViolationCandidate,
)
from services.document_parser import DocumentParser
from services.embedding_provider import DeterministicEmbeddingProvider
from services.local_artifact_storage import LocalArtifactStorage
from services.local_source_storage import LocalSourceStorage

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = tuple(sorted((_PROJECT_ROOT / "sql" / "migrations").glob("*.sql")))


class DeterministicE2EFactProvider:
    """Offline provider that grounds one payment-term fact in the exact source text."""

    def extract(
        self,
        request: FactExtractionRequest | UpdateFactExtractionRequest,
    ) -> FactExtractionResponse:
        for chunk in request.source_chunks:
            for days in (60, 30):
                quote = f"Payment term is {days} days."
                start = chunk.content.find(quote)
                if start >= 0:
                    return FactExtractionResponse(
                        format="fact-extraction-v1",
                        candidates=(
                            FactExtractionCandidate(
                                subject="payment term",
                                predicate="days",
                                object_value=days,
                                evidence=(
                                    FactEvidenceCandidate(
                                        chunk_id=chunk.chunk_id,
                                        relative_start_offset=start,
                                        relative_end_offset=start + len(quote),
                                        quoted_text=quote,
                                    ),
                                ),
                            ),
                        ),
                    )
        return FactExtractionResponse(format="fact-extraction-v1", candidates=())


class DeterministicE2EContradictionProvider:
    def detect(self, _request) -> ContradictionDetectionResponse:
        return ContradictionDetectionResponse(
            format="contradiction-detection-v1",
            contradictions=(),
        )


class DeterministicE2ERuleProvider:
    """Create one grounded violation per selected rule so mixed review is exercised."""

    def evaluate(self, request: RuleEvaluationRequest) -> RuleEvaluationResponse:
        if not request.source_facts:
            return RuleEvaluationResponse(
                format="rule-evaluation-v1",
                outcomes=tuple(
                    RuleEvaluationOutcome(
                        source_rule_id=rule.source_rule_id,
                        outcome=RuleOutcomeType.NO_VIOLATION,
                    )
                    for rule in request.rules
                ),
            )
        supporting_fact_id = request.source_facts[-1].fact_id
        return RuleEvaluationResponse(
            format="rule-evaluation-v1",
            outcomes=tuple(
                RuleEvaluationOutcome(
                    source_rule_id=rule.source_rule_id,
                    outcome=RuleOutcomeType.VIOLATIONS,
                    violations=(
                        RuleViolationCandidate(
                            supporting_fact_ids=(supporting_fact_id,),
                        ),
                    ),
                )
                for rule in request.rules
            ),
        )


@dataclass(frozen=True)
class ExternalE2EHarness:
    database_name: str
    conninfo: str
    pool: ConnectionPool
    app: FastAPI
    mcp_server: object
    source_root: Path
    artifact_root: Path


@dataclass(frozen=True)
class _Services:
    create_pile: CreatePileService
    create_rule_set: CreateRuleSetService
    query: ExternalQueryService
    ingestion: IngestAndReconcileDocumentService
    advance: ExternalAdvanceRunService
    review_query: ExternalReviewQueryService
    review_decision: ReviewDecisionService
    update_review: ExternalUpdateReviewService


def _create_database(database_name: str) -> tuple[str, str]:
    connection_info = conninfo_to_dict(get_settings().database_url)
    connection_info["dbname"] = "postgres"
    admin_conninfo = make_conninfo(**connection_info)
    with connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database_name}"')

    database_info = dict(connection_info)
    database_info["dbname"] = database_name
    database_conninfo = make_conninfo(**database_info)
    with connect(database_conninfo) as connection:
        for migration in _MIGRATIONS:
            connection.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
    return admin_conninfo, database_conninfo


def _drop_database(admin_conninfo: str, database_name: str) -> None:
    with connect(admin_conninfo, autocommit=True) as admin:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


def _services(pool: ConnectionPool, root: Path) -> _Services:
    source_root = root / "sources"
    artifact_root = root / "artifacts"
    artifact_storage = LocalArtifactStorage(artifact_root)
    review_query = ExternalReviewQueryService(pool, artifact_storage)
    workflow = RunWorkflowService(
        pool,
        PrepareUpdateRunService(pool),
        RunRuleScopeService(pool),
        ExtractRunFactsService(
            pool,
            DeterministicE2EFactProvider(),
            "Extract only grounded payment-term facts.",
            "phase6b-e2e-v1",
        ),
        DetectRunContradictionsService(
            pool,
            DeterministicE2EContradictionProvider(),
            "Detect only grounded contradictions.",
            "phase6b-e2e-v1",
        ),
        EvaluateRunRulesService(
            pool,
            DeterministicE2ERuleProvider(),
            "Evaluate captured rules against grounded facts.",
            "phase6b-e2e-v1",
        ),
        CommitFinalReportService(
            pool,
            artifact_storage,
            FinalizeRunService(pool, artifact_storage),
        ),
    )
    return _Services(
        create_pile=CreatePileService(pool),
        create_rule_set=CreateRuleSetService(pool),
        query=ExternalQueryService(pool),
        ingestion=IngestAndReconcileDocumentService(
            IngestDocumentService(
                pool,
                LocalSourceStorage(source_root),
                DocumentParser(),
                DeterministicEmbeddingProvider(),
            ),
            DocumentArrivalService(pool),
        ),
        advance=ExternalAdvanceRunService(review_query, workflow),
        review_query=review_query,
        review_decision=ReviewDecisionService(pool),
        update_review=ExternalUpdateReviewService(
            BuildUpdateChangeSetService(pool),
            ReviewUpdateChangeSetService(pool),
        ),
    )


def _rest_app(pool: ConnectionPool, services: _Services) -> FastAPI:
    app = FastAPI(title="Task 1 E2E")
    install_error_handlers(app)
    app.include_router(api_v1_router)
    app.dependency_overrides.update(
        {
            get_create_pile_service: lambda: services.create_pile,
            get_create_rule_set_service: lambda: services.create_rule_set,
            get_external_query_service: lambda: services.query,
            get_ingest_and_reconcile_document_service: lambda: services.ingestion,
            get_external_advance_run_service: lambda: services.advance,
            get_external_review_query_service: lambda: services.review_query,
            get_review_decision_service: lambda: services.review_decision,
            get_external_update_review_service: lambda: services.update_review,
        },
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        with pool.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    return app


def _mcp_server(services: _Services):
    provider = McpServiceProvider(
        create_pile=lambda: services.create_pile,
        create_rule_set=lambda: services.create_rule_set,
        query=lambda: services.query,
        ingestion=lambda: services.ingestion,
        advance=lambda: services.advance,
        review_query=lambda: services.review_query,
        review_decision=lambda: services.review_decision,
        update_review=lambda: services.update_review,
    )
    return create_mcp_server(provider)


@contextmanager
def external_e2e_harness(prefix: str = "task1_phase6b_e2e") -> Iterator[ExternalE2EHarness]:
    database_name = f"{prefix}_{uuid4().hex}"
    admin_conninfo, database_conninfo = _create_database(database_name)
    with TemporaryDirectory(prefix="task1-phase6b-e2e-") as temporary:
        root = Path(temporary)
        pool = ConnectionPool(database_conninfo, min_size=1, max_size=6, open=False)
        pool.open()
        try:
            services = _services(pool, root)
            yield ExternalE2EHarness(
                database_name=database_name,
                conninfo=database_conninfo,
                pool=pool,
                app=_rest_app(pool, services),
                mcp_server=_mcp_server(services),
                source_root=root / "sources",
                artifact_root=root / "artifacts",
            )
        finally:
            pool.close()
            _drop_database(admin_conninfo, database_name)
