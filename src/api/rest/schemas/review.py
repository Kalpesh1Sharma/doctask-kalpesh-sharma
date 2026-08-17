"""REST v1 DTOs for Phase 6B-3 review and final-report resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, JsonValue

from api.rest.schemas.core import ApiModel, EvidenceResponse
from domain.finding import FindingStatus, FindingType
from domain.review_decision import ReviewDecisionType
from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_review import UpdateReviewDecisionType


class FindingDecisionRequest(ApiModel):
    decision: ReviewDecisionType
    actor_type: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=256)


class FindingDecisionResponse(ApiModel):
    review_decision_id: str
    finding_id: str
    decision: ReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime
    finding_status: FindingStatus
    review_complete: bool
    replayed: bool


class ReviewDecisionResponse(ApiModel):
    review_decision_id: str
    finding_id: str
    decision: ReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime


class ReviewFindingResponse(ApiModel):
    run_id: str
    finding_id: str
    relation: RunMembershipRelation
    origin_run_id: str
    finding_type: FindingType
    rule_id: str | None
    title: str
    description: str | None
    severity: str
    status: FindingStatus
    evidence: list[EvidenceResponse]
    decision: ReviewDecisionResponse | None


class UpdateReviewDecisionResponse(ApiModel):
    update_review_decision_id: str
    change_set_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime


class UpdateChangeSetResponse(ApiModel):
    change_set_id: str | None
    durable: bool
    change_set_fingerprint: str
    manifest: JsonValue
    decision: UpdateReviewDecisionResponse | None


class RunReviewResponse(ApiModel):
    run_id: str
    run_type: RunType
    run_status: RunStatus
    findings: list[ReviewFindingResponse]
    finding_review_complete: bool
    update_change_set: UpdateChangeSetResponse | None
    review_complete: bool
    commit_ready: bool


class UpdateDecisionRequest(ApiModel):
    decision: UpdateReviewDecisionType
    actor_type: str = Field(min_length=1, max_length=100)
    actor_id: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_change_set_fingerprint: str = Field(min_length=71, max_length=71)


class UpdateDecisionResponse(ApiModel):
    change_set_id: str
    change_set_fingerprint: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime
    replayed: bool


class FinalReportResponse(ApiModel):
    output_id: str
    run_id: str
    output_type: str
    storage_uri: str
    content_hash: str
    created_at: datetime
    render_input_fingerprint: str
    focused_update: JsonValue | None
