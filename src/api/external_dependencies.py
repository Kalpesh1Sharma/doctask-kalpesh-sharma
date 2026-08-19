"""Shared production wiring for external REST and MCP application use cases."""

from __future__ import annotations

from functools import lru_cache

from api.application.acknowledge_run_review_service import AcknowledgeRunReviewService
from api.application.build_update_change_set_service import BuildUpdateChangeSetService
from api.application.change_history_service import ChangeHistoryService
from api.application.classify_document_version_service import ClassifyDocumentVersionService
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
from api.application.review_update_change_item_service import ReviewUpdateChangeItemService
from api.application.review_update_change_set_service import ReviewUpdateChangeSetService
from api.application.run_rule_scope_service import RunRuleScopeService
from api.application.run_workflow_service import RunWorkflowService
from api.application.semantic_chunk_retrieval_service import SemanticChunkRetrievalService
from config.settings import get_settings
from db.pool import pool
from services.document_parser import DocumentParser
from services.embedding_provider import DeterministicEmbeddingProvider
from services.groq_document_classification_provider import GroqDocumentClassificationProvider
from services.groq_structured_transport import GroqRateLimiter, GroqStructuredTransport
from services.groq_task_providers import (
    GroqContradictionDetectionProvider,
    GroqFactExtractionProvider,
    GroqRuleEvaluationProvider,
)
from services.local_artifact_storage import LocalArtifactStorage
from services.local_source_storage import LocalSourceStorage

_FACT_INSTRUCTIONS = "Extract only grounded facts supported by the supplied source chunks."
_CONTRADICTION_INSTRUCTIONS = "Detect only contradictions grounded in the supplied facts and evidence."
_RULE_INSTRUCTIONS = "Evaluate only the captured rules against the supplied grounded facts and evidence."
_INSTRUCTION_VERSION = "rest-v1"


class ExternalServiceConfigurationError(RuntimeError):
    pass


@lru_cache
def get_groq_transport() -> GroqStructuredTransport:
    settings = get_settings()
    if not settings.groq_api_key:
        raise ExternalServiceConfigurationError(
            "GROQ_API_KEY is required for semantic document processing",
        )
    limiter = GroqRateLimiter(
        max_requests_per_minute=settings.groq_max_requests_per_minute,
        min_request_interval_seconds=settings.groq_min_request_interval_seconds,
        reserve_tokens=settings.groq_rate_limit_reserve_tokens,
    )
    return GroqStructuredTransport(
        settings.groq_api_key,
        base_url=settings.groq_base_url,
        timeout_seconds=settings.groq_timeout_seconds,
        max_completion_tokens=settings.groq_max_completion_tokens,
        max_retries=settings.groq_max_retries,
        backoff_base_seconds=settings.groq_backoff_base_seconds,
        backoff_max_seconds=settings.groq_backoff_max_seconds,
        estimated_chars_per_token=settings.groq_estimated_chars_per_token,
        rate_limiter=limiter,
    )


@lru_cache
def get_document_classification_service() -> ClassifyDocumentVersionService:
    settings = get_settings()
    return ClassifyDocumentVersionService(
        pool,
        GroqDocumentClassificationProvider(
            get_groq_transport(),
            settings.groq_document_classification_model,
        ),
        max_chunks=settings.document_classification_max_chunks,
        max_chars_per_chunk=settings.document_classification_max_chars_per_chunk,
    )


@lru_cache
def get_create_pile_service() -> CreatePileService:
    return CreatePileService(pool)


@lru_cache
def get_create_rule_set_service() -> CreateRuleSetService:
    return CreateRuleSetService(pool)


@lru_cache
def get_external_query_service() -> ExternalQueryService:
    return ExternalQueryService(pool)


@lru_cache
def get_change_history_service() -> ChangeHistoryService:
    return ChangeHistoryService(pool)


@lru_cache
def get_review_decision_service() -> ReviewDecisionService:
    return ReviewDecisionService(pool)


@lru_cache
def get_acknowledge_run_review_service() -> AcknowledgeRunReviewService:
    return AcknowledgeRunReviewService(pool)


@lru_cache
def get_external_update_review_service() -> ExternalUpdateReviewService:
    return ExternalUpdateReviewService(
        BuildUpdateChangeSetService(pool),
        ReviewUpdateChangeSetService(pool),
        ReviewUpdateChangeItemService(pool),
    )


@lru_cache
def get_external_review_query_service() -> ExternalReviewQueryService:
    settings = get_settings()
    return ExternalReviewQueryService(
        pool,
        LocalArtifactStorage(settings.artifact_root),
    )


@lru_cache
def get_analysis_embedding_provider() -> DeterministicEmbeddingProvider:
    # Local deterministic embeddings keep retrieval rate-limit-free. The
    # production analysis path still performs a real pgvector similarity query;
    # provenance remains grounded in immutable chunk/evidence identities.
    return DeterministicEmbeddingProvider()


@lru_cache
def get_semantic_chunk_retrieval_service() -> SemanticChunkRetrievalService:
    settings = get_settings()
    return SemanticChunkRetrievalService(
        get_analysis_embedding_provider(),
        default_limit=settings.analysis_retrieval_top_k,
    )


@lru_cache
def get_ingest_and_reconcile_document_service() -> IngestAndReconcileDocumentService:
    settings = get_settings()
    arrival = DocumentArrivalService(pool)
    # Groq does not expose an embeddings endpoint. Keep ingestion/retrieval
    # local and rate-limit-free while storing vectors in PostgreSQL/pgvector.
    embedding_provider = get_analysis_embedding_provider()
    ingestion = IngestDocumentService(
        pool,
        LocalSourceStorage(settings.source_root),
        DocumentParser(),
        embedding_provider,
    )
    return IngestAndReconcileDocumentService(
        ingestion,
        arrival,
        get_document_classification_service(),
    )


@lru_cache
def get_external_advance_run_service() -> ExternalAdvanceRunService:
    return ExternalAdvanceRunService(
        get_external_review_query_service(),
        get_run_workflow_service(),
    )


@lru_cache
def get_run_workflow_service() -> RunWorkflowService:
    settings = get_settings()
    transport = get_groq_transport()
    extraction = ExtractRunFactsService(
        pool,
        GroqFactExtractionProvider(transport, settings.groq_fact_extraction_model),
        _FACT_INSTRUCTIONS,
        _INSTRUCTION_VERSION,
        retrieval_service=get_semantic_chunk_retrieval_service(),
        retrieval_query=settings.analysis_retrieval_query,
    )
    contradictions = DetectRunContradictionsService(
        pool,
        GroqContradictionDetectionProvider(transport, settings.groq_contradiction_model),
        _CONTRADICTION_INSTRUCTIONS,
        _INSTRUCTION_VERSION,
    )
    rules = EvaluateRunRulesService(
        pool,
        GroqRuleEvaluationProvider(transport, settings.groq_rule_evaluation_model),
        _RULE_INSTRUCTIONS,
        _INSTRUCTION_VERSION,
    )
    artifact_storage = LocalArtifactStorage(settings.artifact_root)
    finalizer = FinalizeRunService(pool, artifact_storage)
    commit = CommitFinalReportService(pool, artifact_storage, finalizer)
    return RunWorkflowService(
        pool,
        PrepareUpdateRunService(pool),
        RunRuleScopeService(pool),
        extraction,
        contradictions,
        rules,
        commit,
    )
