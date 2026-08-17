"""Phase 6B-3 human-review and final-report REST routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from api.application.external_review_query_service import ExternalReviewQueryService
from api.application.external_update_review_service import (
    ExternalUpdateReviewCommand,
    ExternalUpdateReviewService,
)
from api.application.review_decision_service import (
    ReviewDecisionCommand,
    ReviewDecisionService,
)
from api.rest.dependencies import (
    get_external_review_query_service,
    get_external_update_review_service,
    get_review_decision_service,
)
from api.rest.schemas.core import EvidenceResponse
from api.rest.schemas.review import (
    FinalReportResponse,
    FindingDecisionRequest,
    FindingDecisionResponse,
    ReviewDecisionResponse,
    ReviewFindingResponse,
    RunReviewResponse,
    UpdateChangeSetResponse,
    UpdateDecisionRequest,
    UpdateDecisionResponse,
    UpdateReviewDecisionResponse,
)

router = APIRouter()

ReviewQueryDependency = Annotated[
    ExternalReviewQueryService,
    Depends(get_external_review_query_service),
]
ReviewDecisionDependency = Annotated[
    ReviewDecisionService,
    Depends(get_review_decision_service),
]
UpdateReviewDependency = Annotated[
    ExternalUpdateReviewService,
    Depends(get_external_update_review_service),
]


def _review_response(item: object) -> RunReviewResponse:
    findings = []
    for finding in item.findings:
        decision = (
            ReviewDecisionResponse.model_validate(finding.decision.__dict__)
            if finding.decision is not None
            else None
        )
        findings.append(
            ReviewFindingResponse(
                run_id=finding.run_id,
                finding_id=finding.finding_id,
                relation=finding.relation,
                origin_run_id=finding.origin_run_id,
                finding_type=finding.finding_type,
                rule_id=finding.rule_id,
                title=finding.title,
                description=finding.description,
                severity=finding.severity,
                status=finding.status,
                evidence=[
                    EvidenceResponse.model_validate(evidence.__dict__)
                    for evidence in finding.evidence
                ],
                decision=decision,
            ),
        )

    update_change_set = None
    if item.update_change_set is not None:
        update_decision = (
            UpdateReviewDecisionResponse.model_validate(
                item.update_change_set.decision.__dict__,
            )
            if item.update_change_set.decision is not None
            else None
        )
        update_change_set = UpdateChangeSetResponse(
            change_set_id=item.update_change_set.change_set_id,
            durable=item.update_change_set.durable,
            change_set_fingerprint=item.update_change_set.change_set_fingerprint,
            manifest=item.update_change_set.manifest,
            decision=update_decision,
        )

    return RunReviewResponse(
        run_id=item.run_id,
        run_type=item.run_type,
        run_status=item.run_status,
        findings=findings,
        finding_review_complete=item.finding_review_complete,
        update_change_set=update_change_set,
        review_complete=item.review_complete,
        commit_ready=item.commit_ready,
    )


@router.get("/runs/{run_id}/review", response_model=RunReviewResponse)
def get_run_review(run_id: str, service: ReviewQueryDependency) -> RunReviewResponse:
    return _review_response(service.get_review(run_id))


@router.post(
    "/runs/{run_id}/findings/{finding_id}/decision",
    response_model=FindingDecisionResponse,
)
def review_finding(
    run_id: str,
    finding_id: str,
    payload: FindingDecisionRequest,
    service: ReviewDecisionDependency,
) -> FindingDecisionResponse:
    result = service.record(
        ReviewDecisionCommand(
            run_id=run_id,
            finding_id=finding_id,
            decision=payload.decision,
            actor_type=payload.actor_type,
            actor_id=payload.actor_id,
            idempotency_key=payload.idempotency_key,
        ),
    )
    decision = result.review_decision
    return FindingDecisionResponse(
        review_decision_id=decision.review_decision_id,
        finding_id=decision.finding_id,
        decision=decision.decision,
        actor_type=decision.actor_type,
        actor_id=decision.actor_id,
        idempotency_key=decision.idempotency_key,
        decided_at=decision.created_at,
        finding_status=result.finding.status,
        review_complete=result.review_complete,
        replayed=result.replayed,
    )


@router.post(
    "/runs/{run_id}/update/decision",
    response_model=UpdateDecisionResponse,
)
def review_update(
    run_id: str,
    payload: UpdateDecisionRequest,
    service: UpdateReviewDependency,
) -> UpdateDecisionResponse:
    result = service.record(
        ExternalUpdateReviewCommand(
            run_id=run_id,
            decision=payload.decision,
            actor_type=payload.actor_type,
            actor_id=payload.actor_id,
            idempotency_key=payload.idempotency_key,
            expected_change_set_fingerprint=payload.expected_change_set_fingerprint,
        ),
    )
    decision = result.review.decision
    return UpdateDecisionResponse(
        change_set_id=result.review.change_set.change_set_id,
        change_set_fingerprint=result.review.change_set.change_set_fingerprint,
        decision=decision.decision,
        actor_type=decision.actor_type,
        actor_id=decision.actor_id,
        idempotency_key=decision.idempotency_key,
        decided_at=decision.decided_at,
        replayed=result.review.replayed,
    )


@router.get("/runs/{run_id}/report", response_model=FinalReportResponse)
def get_run_report(run_id: str, service: ReviewQueryDependency) -> FinalReportResponse:
    return FinalReportResponse.model_validate(service.get_report(run_id).__dict__)


@router.get("/runs/{run_id}/report/content", response_class=Response)
def get_run_report_content(run_id: str, service: ReviewQueryDependency) -> Response:
    report, content = service.read_report_content(run_id)
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "ETag": f'"{report.content_hash}"',
            "X-Content-SHA256": report.content_hash,
        },
    )
