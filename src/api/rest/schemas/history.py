"""REST v1 DTOs for durable committed UPDATE change history."""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue

from api.rest.schemas.core import ApiModel
from domain.change_history import ChangeHistoryOutcome
from domain.document_classification import DocumentRole
from domain.fact import FactStatus
from domain.update_preparation import UpdateDocumentChangeKind
from domain.update_review import UpdateChangeItemKind, UpdateReviewDecisionType


class ChangeHistorySourceResponse(ApiModel):
    pile_id: str
    document_id: str
    logical_key: str
    document_version_id: str
    version_number: int
    content_hash: str
    mime_type: str | None
    role: DocumentRole | None


class ChangeHistoryDocumentChangeResponse(ApiModel):
    document_id: str
    kind: UpdateDocumentChangeKind
    baseline_source: ChangeHistorySourceResponse | None
    target_source: ChangeHistorySourceResponse


class ChangeHistoryEvidenceResponse(ApiModel):
    evidence_id: str
    source: ChangeHistorySourceResponse
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str


class ChangeHistoryFactResponse(ApiModel):
    fact_id: str
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus


class ChangeHistoryReviewDecisionResponse(ApiModel):
    decision_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    decided_at: datetime


class ChangeHistoryCommitActorResponse(ApiModel):
    actor_type: str
    actor_id: str


class ChangeHistoryItemResponse(ApiModel):
    change_item_id: str
    kind: UpdateChangeItemKind
    outcome: ChangeHistoryOutcome
    fact: ChangeHistoryFactResponse
    review: ChangeHistoryReviewDecisionResponse
    evidence: list[ChangeHistoryEvidenceResponse]


class ChangeHistoryRunResponse(ApiModel):
    run_id: str
    pile_id: str
    baseline_run_id: str
    change_set_id: str
    trigger_source: ChangeHistorySourceResponse
    committed_at: datetime
    output_id: str
    output_content_hash: str
    commit_actor: ChangeHistoryCommitActorResponse | None
    document_changes: list[ChangeHistoryDocumentChangeResponse]
    items: list[ChangeHistoryItemResponse]
    whole_update_review: ChangeHistoryReviewDecisionResponse | None
