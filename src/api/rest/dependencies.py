"""REST compatibility exports for shared external application-service wiring."""

from api.external_dependencies import (
    ExternalServiceConfigurationError,
    get_create_pile_service,
    get_create_rule_set_service,
    get_external_advance_run_service,
    get_external_query_service,
    get_external_review_query_service,
    get_external_update_review_service,
    get_ingest_and_reconcile_document_service,
    get_review_decision_service,
    get_run_workflow_service,
)

RestServiceConfigurationError = ExternalServiceConfigurationError

__all__ = [
    "RestServiceConfigurationError",
    "get_create_pile_service",
    "get_create_rule_set_service",
    "get_external_advance_run_service",
    "get_external_query_service",
    "get_external_review_query_service",
    "get_external_update_review_service",
    "get_ingest_and_reconcile_document_service",
    "get_review_decision_service",
    "get_run_workflow_service",
]
