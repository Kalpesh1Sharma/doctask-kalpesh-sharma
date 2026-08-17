"""Lazy shared-service provider used by the MCP tool boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from api.application.create_pile_service import CreatePileService
from api.application.create_rule_set_service import CreateRuleSetService
from api.application.external_advance_run_service import ExternalAdvanceRunService
from api.application.external_query_service import ExternalQueryService
from api.application.external_review_query_service import ExternalReviewQueryService
from api.application.external_update_review_service import ExternalUpdateReviewService
from api.application.ingest_and_reconcile_document_service import IngestAndReconcileDocumentService
from api.application.review_decision_service import ReviewDecisionService
from api.external_dependencies import (
    get_create_pile_service,
    get_create_rule_set_service,
    get_external_advance_run_service,
    get_external_query_service,
    get_external_review_query_service,
    get_external_update_review_service,
    get_ingest_and_reconcile_document_service,
    get_review_decision_service,
)


@dataclass(frozen=True)
class McpServiceProvider:
    """Resolve application services lazily so MCP startup needs no LLM credential."""

    create_pile: Callable[[], CreatePileService]
    create_rule_set: Callable[[], CreateRuleSetService]
    query: Callable[[], ExternalQueryService]
    ingestion: Callable[[], IngestAndReconcileDocumentService]
    advance: Callable[[], ExternalAdvanceRunService]
    review_query: Callable[[], ExternalReviewQueryService]
    review_decision: Callable[[], ReviewDecisionService]
    update_review: Callable[[], ExternalUpdateReviewService]


def default_mcp_service_provider() -> McpServiceProvider:
    return McpServiceProvider(
        create_pile=get_create_pile_service,
        create_rule_set=get_create_rule_set_service,
        query=get_external_query_service,
        ingestion=get_ingest_and_reconcile_document_service,
        advance=get_external_advance_run_service,
        review_query=get_external_review_query_service,
        review_decision=get_review_decision_service,
        update_review=get_external_update_review_service,
    )
