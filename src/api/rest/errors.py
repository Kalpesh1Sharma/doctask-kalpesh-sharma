"""Transport-level REST error-envelope helpers and application mappings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.application.acknowledge_run_review_service import (
    InvalidRunReviewAcknowledgementKeyError,
    RunReviewAcknowledgementConflictError,
    RunReviewAcknowledgementIntegrityError,
    RunReviewAcknowledgementNotEligibleError,
    RunReviewAcknowledgementRunNotFoundError,
)
from api.application.build_update_change_set_service import (
    UpdateChangeSetConflictError,
    UpdateChangeSetPreparationError,
    UpdateChangeSetReviewIncompleteError,
    UpdateChangeSetRuleScopeError,
    UpdateChangeSetRunNotEligibleError,
    UpdateChangeSetRunNotFoundError,
)
from api.application.change_history_service import (
    ChangeHistoryIntegrityError,
    ChangeHistoryPileNotFoundError,
    ChangeHistoryRunNotFoundError,
    InvalidChangeHistoryQueryError,
)
from api.application.classify_document_version_service import (
    DocumentClassificationClaimLostError,
    DocumentClassificationInProgressError,
    DocumentClassificationIntegrityError,
    DocumentClassificationInvalidResponseError,
    DocumentClassificationProviderFailureError,
    DocumentClassificationVersionNotFoundError,
    DocumentClassificationVersionNotReadyError,
    InvalidDocumentClassificationCommandError,
)
from api.application.commit_final_report_service import (
    CommitArtifactPublicationError,
    CommitFinalReportClaimLostError,
    CommitFinalReportInProgressError,
    CommitFinalReportInputDriftError,
    CommitFinalReportIntegrityError,
    CommitFinalReportNotEligibleError,
    CommitFinalReportNotFoundError,
    CommitFinalReportPreviouslyFailedError,
    InvalidCommitFinalReportCommandError,
)
from api.application.create_pile_service import InvalidCreatePileCommandError
from api.application.create_rule_set_service import (
    CreateRuleSetPileNotFoundError,
    InvalidCreateRuleSetCommandError,
)
from api.application.document_arrival_service import (
    DocumentArrivalPileNotFoundError,
    PileTailIntegrityError,
)
from api.application.external_advance_run_service import ExternalHumanReviewRequiredError
from api.application.external_query_service import (
    DocumentNotFoundError,
    InvalidExternalQueryError,
    PileNotFoundError,
    RuleSetNotFoundExternalError,
    RunNotFoundExternalError,
)
from api.application.external_review_query_service import (
    ExternalReviewIntegrityError,
    ExternalReviewRunNotFoundError,
    FinalReportIntegrityError,
    FinalReportNotFoundError,
    InvalidExternalReviewQueryError,
)
from api.application.external_update_review_service import (
    ExternalUpdateReviewItemRequiredError,
    ExternalUpdateReviewStaleError,
    InvalidExternalUpdateReviewCommandError,
)
from api.application.finalize_run_service import (
    FinalArtifactIntegrityError,
    FinalizationIntegrityError,
    MissingRunRuleScopeError,
    RenderInputDriftError,
    ReviewIncompleteError,
    UpdateApprovalMissingError,
    UpdateApprovalRejectedError,
    UpdateApprovalStaleError,
)
from api.application.ingest_document_service import (
    DocumentIngestionClaimLostError,
    DocumentIngestionInProgressError,
    DocumentIngestionInputConflictError,
    DocumentIngestionIntegrityError,
    DocumentIngestionPileNotFoundError,
    DocumentIngestionPreviouslyFailedError,
    DocumentIngestionProcessingError,
    DocumentIngestionProviderFailureError,
    DocumentIngestionUnsupportedSourceError,
    InvalidDocumentIngestionCommandError,
)
from api.application.review_decision_service import (
    FindingNotFoundError,
    FindingNotInRunError,
    InvalidReviewIdempotencyKeyError,
    ReviewIdempotencyConflictError,
    RunNotReviewableError,
    TerminalFindingError,
)
from api.application.review_decision_service import (
    RunNotFoundError as ReviewRunNotFoundError,
)
from api.application.review_update_change_item_service import (
    UpdateItemAlreadyReviewedError,
    UpdateItemReviewChangeSetMissingError,
    UpdateItemReviewChangeSetStaleError,
    UpdateItemReviewIdempotencyConflictError,
    UpdateItemReviewIdempotencyKeyError,
    UpdateItemReviewItemNotFoundError,
    UpdateItemReviewRunNotEligibleError,
    UpdateItemReviewRunNotFoundError,
)
from api.application.review_update_change_set_service import (
    UpdateChangeSetAlreadyReviewedError,
    UpdateChangeSetStaleError,
    UpdateReviewChangeSetMissingError,
    UpdateReviewIdempotencyConflictError,
    UpdateReviewIdempotencyKeyError,
    UpdateReviewRunNotEligibleError,
    UpdateReviewRunNotFoundError,
)
from api.application.run_rule_scope_service import (
    InvalidRunRuleScopeSelectionError,
    RuleNotFoundError,
    RuleSelectionMismatchError,
    RuleSetNotFoundError,
    RuleSetPileMismatchError,
    RunRuleScopeConflictError,
    RunRuleScopeRunNotEligibleError,
)
from api.application.run_workflow_service import (
    InvalidRunWorkflowCommandError,
    RunWorkflowAwaitingUpstream,
    RunWorkflowGraphIntegrityError,
    RunWorkflowOperationInProgress,
    RunWorkflowRuleScopeSelectionRequired,
    RunWorkflowRunFailedError,
    RunWorkflowRunNotFoundError,
    RunWorkflowStepEscalatedError,
    RunWorkflowUnmanagedRunError,
)
from api.rest.dependencies import RestServiceConfigurationError
from api.rest.schemas.common import ApiError, ApiErrorResponse
from domain.update_review import (
    UpdateChangeSetInputIntegrityError,
    UpdateChangeSetIntegrityError,
)
from services.local_source_storage import (
    SourceIntegrityError,
    SourcePublicationError,
    SourceTooLargeError,
)

ErrorHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def api_error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    payload = ApiErrorResponse(
        error=ApiError(code=code, message=message, details=details or {}),
    )
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload))


def _simple_handler(status_code: int, code: str) -> ErrorHandler:
    async def handler(_request: Request, error: Exception) -> JSONResponse:
        return api_error_response(status_code, code, str(error))

    return handler


def _integrity_handler(code: str) -> ErrorHandler:
    async def handler(_request: Request, _error: Exception) -> JSONResponse:
        return api_error_response(500, code, "durable state failed an integrity check")

    return handler


async def _workflow_state_handler(_request: Request, error: Exception) -> JSONResponse:
    state = error
    details = {
        "run_id": getattr(state, "run_id", None),
        "stage": getattr(getattr(state, "stage", None), "value", None),
        "attempt": getattr(state, "attempt", None),
        "operation_scope": getattr(state, "operation_scope", None),
        "operation_key": getattr(state, "operation_key", None),
        "claim_generation": getattr(state, "claim_generation", None),
    }
    details = {key: value for key, value in details.items() if value is not None}
    return api_error_response(
        409,
        str(getattr(state, "error_code", "RUN_WORKFLOW_STATE_CONFLICT")),
        str(error),
        details=details,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return api_error_response(
            422,
            "REQUEST_VALIDATION_ERROR",
            "request validation failed",
            details={"validation_errors": jsonable_encoder(error.errors())},
        )

    mappings: tuple[tuple[type[Exception], ErrorHandler], ...] = (
        (
            InvalidChangeHistoryQueryError,
            _simple_handler(400, "INVALID_CHANGE_HISTORY_QUERY"),
        ),
        (ChangeHistoryPileNotFoundError, _simple_handler(404, "PILE_NOT_FOUND")),
        (ChangeHistoryRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (ChangeHistoryIntegrityError, _integrity_handler("CHANGE_HISTORY_INTEGRITY")),
        (
            InvalidDocumentClassificationCommandError,
            _simple_handler(400, "INVALID_DOCUMENT_CLASSIFICATION"),
        ),
        (
            DocumentClassificationVersionNotFoundError,
            _simple_handler(404, "DOCUMENT_VERSION_NOT_FOUND"),
        ),
        (
            DocumentClassificationVersionNotReadyError,
            _simple_handler(409, "DOCUMENT_VERSION_NOT_READY"),
        ),
        (
            DocumentClassificationInProgressError,
            _simple_handler(409, "DOCUMENT_CLASSIFICATION_IN_PROGRESS"),
        ),
        (
            DocumentClassificationClaimLostError,
            _simple_handler(409, "DOCUMENT_CLASSIFICATION_CLAIM_LOST"),
        ),
        (
            DocumentClassificationProviderFailureError,
            _simple_handler(503, "DOCUMENT_CLASSIFICATION_PROVIDER_FAILURE"),
        ),
        (
            DocumentClassificationInvalidResponseError,
            _simple_handler(503, "DOCUMENT_CLASSIFICATION_INVALID_RESPONSE"),
        ),
        (
            DocumentClassificationIntegrityError,
            _integrity_handler("DOCUMENT_CLASSIFICATION_INTEGRITY"),
        ),
        (RestServiceConfigurationError, _simple_handler(503, "SERVICE_NOT_CONFIGURED")),
        (InvalidCreatePileCommandError, _simple_handler(400, "INVALID_PILE")),
        (InvalidCreateRuleSetCommandError, _simple_handler(400, "INVALID_RULE_SET")),
        (CreateRuleSetPileNotFoundError, _simple_handler(404, "PILE_NOT_FOUND")),
        (InvalidExternalQueryError, _simple_handler(400, "INVALID_QUERY")),
        (PileNotFoundError, _simple_handler(404, "PILE_NOT_FOUND")),
        (DocumentNotFoundError, _simple_handler(404, "DOCUMENT_NOT_FOUND")),
        (RuleSetNotFoundExternalError, _simple_handler(404, "RULE_SET_NOT_FOUND")),
        (RunNotFoundExternalError, _simple_handler(404, "RUN_NOT_FOUND")),
        (ExternalHumanReviewRequiredError, _simple_handler(409, "HUMAN_REVIEW_REQUIRED")),
        (InvalidExternalReviewQueryError, _simple_handler(400, "INVALID_REVIEW_QUERY")),
        (ExternalReviewRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (ExternalReviewIntegrityError, _integrity_handler("REVIEW_STATE_INTEGRITY")),
        (FinalReportNotFoundError, _simple_handler(404, "FINAL_REPORT_NOT_FOUND")),
        (FinalReportIntegrityError, _integrity_handler("FINAL_REPORT_INTEGRITY")),
        (
            InvalidRunReviewAcknowledgementKeyError,
            _simple_handler(400, "INVALID_RUN_REVIEW_ACKNOWLEDGEMENT_KEY"),
        ),
        (
            RunReviewAcknowledgementRunNotFoundError,
            _simple_handler(404, "RUN_NOT_FOUND"),
        ),
        (
            RunReviewAcknowledgementNotEligibleError,
            _simple_handler(409, "RUN_REVIEW_ACKNOWLEDGEMENT_NOT_ELIGIBLE"),
        ),
        (
            RunReviewAcknowledgementConflictError,
            _simple_handler(409, "RUN_REVIEW_ACKNOWLEDGEMENT_CONFLICT"),
        ),
        (
            RunReviewAcknowledgementIntegrityError,
            _integrity_handler("RUN_REVIEW_ACKNOWLEDGEMENT_INTEGRITY"),
        ),
        (InvalidReviewIdempotencyKeyError, _simple_handler(400, "INVALID_REVIEW_IDEMPOTENCY_KEY")),
        (ReviewRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (FindingNotFoundError, _simple_handler(404, "FINDING_NOT_FOUND")),
        (FindingNotInRunError, _simple_handler(409, "FINDING_NOT_IN_RUN")),
        (RunNotReviewableError, _simple_handler(409, "RUN_NOT_REVIEWABLE")),
        (TerminalFindingError, _simple_handler(409, "FINDING_ALREADY_RESOLVED")),
        (ReviewIdempotencyConflictError, _simple_handler(409, "REVIEW_IDEMPOTENCY_CONFLICT")),
        (InvalidExternalUpdateReviewCommandError, _simple_handler(400, "INVALID_UPDATE_REVIEW")),
        (ExternalUpdateReviewStaleError, _simple_handler(409, "UPDATE_REVIEW_STALE")),
        (
            ExternalUpdateReviewItemRequiredError,
            _simple_handler(409, "UPDATE_ITEM_REVIEW_REQUIRED"),
        ),
        (UpdateChangeSetRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (UpdateChangeSetRunNotEligibleError, _simple_handler(409, "UPDATE_REVIEW_NOT_ELIGIBLE")),
        (UpdateChangeSetReviewIncompleteError, _simple_handler(409, "FINDING_REVIEW_INCOMPLETE")),
        (UpdateChangeSetConflictError, _simple_handler(409, "UPDATE_CHANGE_SET_CONFLICT")),
        (UpdateChangeSetPreparationError, _integrity_handler("UPDATE_PREPARATION_INTEGRITY")),
        (UpdateChangeSetRuleScopeError, _integrity_handler("RULE_SCOPE_INTEGRITY")),
        (UpdateChangeSetInputIntegrityError, _integrity_handler("UPDATE_CHANGE_SET_INTEGRITY")),
        (UpdateChangeSetIntegrityError, _integrity_handler("UPDATE_CHANGE_SET_INTEGRITY")),
        (UpdateReviewRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (UpdateReviewChangeSetMissingError, _simple_handler(409, "UPDATE_CHANGE_SET_MISSING")),
        (
            UpdateReviewIdempotencyKeyError,
            _simple_handler(400, "INVALID_UPDATE_REVIEW_IDEMPOTENCY_KEY"),
        ),
        (
            UpdateReviewIdempotencyConflictError,
            _simple_handler(409, "UPDATE_REVIEW_IDEMPOTENCY_CONFLICT"),
        ),
        (UpdateChangeSetAlreadyReviewedError, _simple_handler(409, "UPDATE_ALREADY_REVIEWED")),
        (UpdateReviewRunNotEligibleError, _simple_handler(409, "RUN_NOT_REVIEWABLE")),
        (UpdateChangeSetStaleError, _simple_handler(409, "UPDATE_CHANGE_SET_STALE")),
        (UpdateItemReviewRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (
            UpdateItemReviewChangeSetMissingError,
            _simple_handler(409, "UPDATE_CHANGE_SET_MISSING"),
        ),
        (UpdateItemReviewItemNotFoundError, _simple_handler(404, "UPDATE_ITEM_NOT_FOUND")),
        (
            UpdateItemReviewIdempotencyKeyError,
            _simple_handler(400, "INVALID_UPDATE_ITEM_REVIEW_IDEMPOTENCY_KEY"),
        ),
        (
            UpdateItemReviewIdempotencyConflictError,
            _simple_handler(409, "UPDATE_ITEM_REVIEW_IDEMPOTENCY_CONFLICT"),
        ),
        (UpdateItemAlreadyReviewedError, _simple_handler(409, "UPDATE_ITEM_ALREADY_REVIEWED")),
        (UpdateItemReviewRunNotEligibleError, _simple_handler(409, "RUN_NOT_REVIEWABLE")),
        (UpdateItemReviewChangeSetStaleError, _simple_handler(409, "UPDATE_CHANGE_SET_STALE")),
        (InvalidDocumentIngestionCommandError, _simple_handler(400, "INVALID_DOCUMENT_INGESTION")),
        (DocumentIngestionPileNotFoundError, _simple_handler(404, "PILE_NOT_FOUND")),
        (DocumentIngestionInProgressError, _simple_handler(409, "DOCUMENT_INGESTION_IN_PROGRESS")),
        (DocumentIngestionPreviouslyFailedError, _simple_handler(409, "DOCUMENT_INGESTION_PREVIOUSLY_FAILED")),
        (DocumentIngestionInputConflictError, _simple_handler(409, "DOCUMENT_INGESTION_CONFLICT")),
        (DocumentIngestionClaimLostError, _simple_handler(409, "DOCUMENT_INGESTION_CLAIM_LOST")),
        (DocumentIngestionUnsupportedSourceError, _simple_handler(415, "UNSUPPORTED_SOURCE")),
        (DocumentIngestionProviderFailureError, _simple_handler(503, "EMBEDDING_PROVIDER_FAILURE")),
        (DocumentIngestionProcessingError, _simple_handler(415, "SOURCE_PROCESSING_FAILED")),
        (DocumentIngestionIntegrityError, _integrity_handler("DOCUMENT_INGESTION_INTEGRITY")),
        (DocumentArrivalPileNotFoundError, _simple_handler(404, "PILE_NOT_FOUND")),
        (PileTailIntegrityError, _integrity_handler("PILE_LINEAGE_INTEGRITY")),
        (SourceTooLargeError, _simple_handler(413, "SOURCE_TOO_LARGE")),
        (SourcePublicationError, _simple_handler(503, "SOURCE_PUBLICATION_FAILED")),
        (SourceIntegrityError, _integrity_handler("SOURCE_INTEGRITY")),
        (InvalidCommitFinalReportCommandError, _simple_handler(400, "INVALID_COMMIT")),
        (CommitFinalReportNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (CommitFinalReportNotEligibleError, _simple_handler(409, "COMMIT_NOT_ELIGIBLE")),
        (CommitFinalReportInProgressError, _simple_handler(409, "COMMIT_IN_PROGRESS")),
        (CommitFinalReportPreviouslyFailedError, _simple_handler(409, "COMMIT_PREVIOUSLY_FAILED")),
        (CommitFinalReportInputDriftError, _simple_handler(409, "COMMIT_INPUT_DRIFT")),
        (CommitFinalReportClaimLostError, _simple_handler(409, "COMMIT_CLAIM_LOST")),
        (CommitArtifactPublicationError, _simple_handler(503, "ARTIFACT_PUBLICATION_FAILED")),
        (CommitFinalReportIntegrityError, _integrity_handler("COMMIT_INTEGRITY")),
        (ReviewIncompleteError, _simple_handler(409, "FINDING_REVIEW_INCOMPLETE")),
        (UpdateApprovalMissingError, _simple_handler(409, "UPDATE_APPROVAL_MISSING")),
        (UpdateApprovalRejectedError, _simple_handler(409, "UPDATE_APPROVAL_REJECTED")),
        (UpdateApprovalStaleError, _simple_handler(409, "UPDATE_APPROVAL_STALE")),
        (RenderInputDriftError, _simple_handler(409, "RENDER_INPUT_DRIFT")),
        (MissingRunRuleScopeError, _integrity_handler("RULE_SCOPE_INTEGRITY")),
        (FinalArtifactIntegrityError, _integrity_handler("FINAL_ARTIFACT_INTEGRITY")),
        (FinalizationIntegrityError, _integrity_handler("FINALIZATION_INTEGRITY")),
        (InvalidRunWorkflowCommandError, _simple_handler(400, "INVALID_RUN_ADVANCE")),
        (RunWorkflowRunNotFoundError, _simple_handler(404, "RUN_NOT_FOUND")),
        (RunWorkflowRuleScopeSelectionRequired, _simple_handler(400, "RULE_SCOPE_REQUIRED")),
        (RunWorkflowUnmanagedRunError, _simple_handler(409, "RUN_NOT_MANAGED")),
        (RunWorkflowOperationInProgress, _workflow_state_handler),
        (RunWorkflowStepEscalatedError, _workflow_state_handler),
        (RunWorkflowRunFailedError, _workflow_state_handler),
        (RunWorkflowAwaitingUpstream, _workflow_state_handler),
        (RunWorkflowGraphIntegrityError, _integrity_handler("RUN_WORKFLOW_INTEGRITY")),
        (InvalidRunRuleScopeSelectionError, _simple_handler(400, "INVALID_RULE_SCOPE")),
        (RuleSetNotFoundError, _simple_handler(404, "RULE_SET_NOT_FOUND")),
        (RuleNotFoundError, _simple_handler(404, "RULE_NOT_FOUND")),
        (RuleSetPileMismatchError, _simple_handler(409, "RULE_SET_PILE_MISMATCH")),
        (RuleSelectionMismatchError, _simple_handler(409, "RULE_SELECTION_MISMATCH")),
        (RunRuleScopeConflictError, _simple_handler(409, "RULE_SCOPE_CONFLICT")),
        (RunRuleScopeRunNotEligibleError, _simple_handler(409, "RUN_NOT_RULE_SCOPE_ELIGIBLE")),
    )
    for exception_type, handler in mappings:
        app.add_exception_handler(exception_type, handler)
