"""Phase 6B-2 core REST routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from api.application.create_pile_service import CreatePileCommand, CreatePileService
from api.application.create_rule_set_service import (
    CreateRuleCommand,
    CreateRuleSetCommand,
    CreateRuleSetService,
)
from api.application.external_advance_run_service import ExternalAdvanceRunService
from api.application.external_query_service import ExternalQueryService
from api.application.ingest_and_reconcile_document_service import (
    IngestAndReconcileDocumentCommand,
    IngestAndReconcileDocumentService,
)
from api.application.run_workflow_service import RunWorkflowCommand
from api.rest.dependencies import (
    get_create_pile_service,
    get_create_rule_set_service,
    get_external_advance_run_service,
    get_external_query_service,
    get_ingest_and_reconcile_document_service,
)
from api.rest.schemas.core import (
    AdvanceRunRequest,
    AdvanceRunResponse,
    CreatePileRequest,
    CreateRuleSetRequest,
    DocumentResponse,
    DocumentVersionResponse,
    EvidenceResponse,
    FactResponse,
    FindingResponse,
    IngestDocumentResponse,
    PileResponse,
    RuleResponse,
    RuleSetResponse,
    RunMetricResponse,
    RunResponse,
    RunStepResponse,
)

router = APIRouter()

CreatePileDependency = Annotated[CreatePileService, Depends(get_create_pile_service)]
CreateRuleSetDependency = Annotated[CreateRuleSetService, Depends(get_create_rule_set_service)]
QueryDependency = Annotated[ExternalQueryService, Depends(get_external_query_service)]
IngestionDependency = Annotated[
    IngestAndReconcileDocumentService,
    Depends(get_ingest_and_reconcile_document_service),
]
WorkflowDependency = Annotated[ExternalAdvanceRunService, Depends(get_external_advance_run_service)]
Limit = Annotated[int, Query(ge=1, le=200)]


def _pile_response(item: object) -> PileResponse:
    return PileResponse.model_validate(item.__dict__)


def _rule_response(item: object) -> RuleResponse:
    return RuleResponse.model_validate(item.__dict__)


def _rule_set_response(item: object) -> RuleSetResponse:
    payload = dict(item.__dict__)
    payload["rules"] = [_rule_response(rule) for rule in payload["rules"]]
    return RuleSetResponse.model_validate(payload)


def _document_response(item: object) -> DocumentResponse:
    return DocumentResponse.model_validate(item.__dict__)


def _document_version_response(item: object) -> DocumentVersionResponse:
    return DocumentVersionResponse.model_validate(item.__dict__)


def _evidence_response(item: object) -> EvidenceResponse:
    return EvidenceResponse.model_validate(item.__dict__)


def _fact_response(item: object) -> FactResponse:
    payload = dict(item.__dict__)
    payload["evidence"] = [_evidence_response(evidence) for evidence in payload["evidence"]]
    return FactResponse.model_validate(payload)


def _finding_response(item: object) -> FindingResponse:
    payload = dict(item.__dict__)
    payload["evidence"] = [_evidence_response(evidence) for evidence in payload["evidence"]]
    return FindingResponse.model_validate(payload)


def _run_step_response(item: object) -> RunStepResponse:
    return RunStepResponse.model_validate(item.__dict__)


def _run_response(item: object) -> RunResponse:
    payload = dict(item.__dict__)
    payload["steps"] = [_run_step_response(step) for step in payload["steps"]]
    return RunResponse.model_validate(payload)


@router.post("/piles", response_model=PileResponse, status_code=status.HTTP_201_CREATED)
def create_pile(payload: CreatePileRequest, service: CreatePileDependency) -> PileResponse:
    created = service.create(
        CreatePileCommand(
            name=payload.name,
            description=payload.description,
            watch_enabled=payload.watch_enabled,
            watch_location=payload.watch_location,
        ),
    )
    return _pile_response(created)


@router.get("/piles/{pile_id}", response_model=PileResponse)
def get_pile(pile_id: str, service: QueryDependency) -> PileResponse:
    return _pile_response(service.get_pile(pile_id))


@router.get("/piles/{pile_id}/documents", response_model=list[DocumentResponse])
def list_pile_documents(
    pile_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[DocumentResponse]:
    return [_document_response(item) for item in service.list_documents(pile_id, limit=limit)]


@router.post(
    "/piles/{pile_id}/rule-sets",
    response_model=RuleSetResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_rule_set(
    pile_id: str,
    payload: CreateRuleSetRequest,
    service: CreateRuleSetDependency,
) -> RuleSetResponse:
    result = service.create(
        CreateRuleSetCommand(
            pile_id=pile_id,
            name=payload.name,
            description=payload.description,
            version=payload.version,
            rules=tuple(
                CreateRuleCommand(
                    rule_key=item.rule_key,
                    title=item.title,
                    description=item.description,
                    severity=item.severity,
                    definition=item.definition,
                )
                for item in payload.rules
            ),
        ),
    )
    return RuleSetResponse(
        **result.rule_set.__dict__,
        rules=[_rule_response(rule) for rule in result.rules],
    )


@router.get("/piles/{pile_id}/rule-sets", response_model=list[RuleSetResponse])
def list_rule_sets(
    pile_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[RuleSetResponse]:
    return [_rule_set_response(item) for item in service.list_rule_sets(pile_id, limit=limit)]


@router.get("/rule-sets/{rule_set_id}", response_model=RuleSetResponse)
def get_rule_set(rule_set_id: str, service: QueryDependency) -> RuleSetResponse:
    return _rule_set_response(service.get_rule_set(rule_set_id))


@router.post(
    "/piles/{pile_id}/documents",
    response_model=IngestDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
def ingest_document(
    pile_id: str,
    service: IngestionDependency,
    logical_key: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> IngestDocumentResponse:
    result = service.ingest(
        IngestAndReconcileDocumentCommand(
            pile_id=pile_id,
            logical_key=logical_key,
            source=file.file,
        ),
    )
    return IngestDocumentResponse(
        document_id=result.document_id,
        document_version_id=result.document_version_id,
        content_hash=result.content_hash,
        storage_uri=result.storage_uri,
        replayed=result.replayed,
        created_run_ids=list(result.created_run_ids),
        reconciled_blocked_run_id=result.reconciled_blocked_run_id,
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, service: QueryDependency) -> DocumentResponse:
    return _document_response(service.get_document(document_id))


@router.get(
    "/documents/{document_id}/versions",
    response_model=list[DocumentVersionResponse],
)
def list_document_versions(
    document_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[DocumentVersionResponse]:
    return [
        _document_version_response(item)
        for item in service.list_document_versions(document_id, limit=limit)
    ]


@router.get("/piles/{pile_id}/runs", response_model=list[RunResponse])
def list_pile_runs(
    pile_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[RunResponse]:
    return [_run_response(item) for item in service.list_runs(pile_id, limit=limit)]


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, service: QueryDependency) -> RunResponse:
    return _run_response(service.get_run(run_id))


@router.post("/runs/{run_id}/advance", response_model=AdvanceRunResponse)
def advance_run(
    run_id: str,
    payload: AdvanceRunRequest,
    service: WorkflowDependency,
) -> AdvanceRunResponse:
    result = service.advance(
        RunWorkflowCommand(
            run_id=run_id,
            source_rule_set_ids=(
                tuple(payload.source_rule_set_ids)
                if payload.source_rule_set_ids is not None
                else None
            ),
            source_rule_ids=(
                tuple(payload.source_rule_ids)
                if payload.source_rule_ids is not None
                else None
            ),
        ),
    )
    return AdvanceRunResponse(
        run_id=result.run_id,
        stage=result.stage,
        attempt=result.step.attempt,
        step_status=result.step.status,
        run_status=result.run_status,
        outcome=result.outcome.value,
        advanced=result.advanced,
        successor_attempt=result.successor_attempt,
    )


@router.get("/runs/{run_id}/facts", response_model=list[FactResponse])
def get_run_facts(
    run_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[FactResponse]:
    return [_fact_response(item) for item in service.list_run_facts(run_id, limit=limit)]


@router.get("/runs/{run_id}/findings", response_model=list[FindingResponse])
def get_run_findings(
    run_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[FindingResponse]:
    return [_finding_response(item) for item in service.list_run_findings(run_id, limit=limit)]


@router.get("/runs/{run_id}/metrics", response_model=list[RunMetricResponse])
def get_run_metrics(
    run_id: str,
    service: QueryDependency,
    limit: Limit = 100,
) -> list[RunMetricResponse]:
    return [
        RunMetricResponse.model_validate(item.__dict__)
        for item in service.list_run_metrics(run_id, limit=limit)
    ]
