"""REST v1 DTOs for Phase 6B-2 core resources."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from domain.document_classification import DocumentRole
from domain.document_version import DocumentProcessingStatus
from domain.fact import FactStatus
from domain.finding import FindingStatus, FindingType
from domain.run import RunStage, RunStatus, RunStepStatus, RunType
from domain.run_membership import RunMembershipRelation


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePileRequest(ApiModel):
    name: str
    description: str | None = None
    watch_enabled: bool = False
    watch_location: str | None = None


class PileResponse(ApiModel):
    pile_id: str
    name: str
    description: str | None
    watch_enabled: bool
    watch_location: str | None
    latest_run_id: str | None
    created_at: datetime
    updated_at: datetime


class CreateRuleRequest(ApiModel):
    rule_key: str
    title: str
    description: str | None = None
    severity: str
    definition: JsonValue


class CreateRuleSetRequest(ApiModel):
    name: str
    description: str | None = None
    version: int
    rules: list[CreateRuleRequest] = Field(min_length=1, max_length=200)


class RuleResponse(ApiModel):
    rule_id: str
    rule_set_id: str
    rule_key: str
    title: str
    description: str | None
    severity: str
    definition: JsonValue
    created_at: datetime


class RuleSetResponse(ApiModel):
    rule_set_id: str
    pile_id: str
    name: str
    description: str | None
    version: int
    created_at: datetime
    rules: list[RuleResponse]


class DocumentResponse(ApiModel):
    document_id: str
    pile_id: str
    logical_key: str
    current_version_id: str | None
    created_at: datetime
    updated_at: datetime


class DocumentVersionResponse(ApiModel):
    document_version_id: str
    document_id: str
    version_number: int
    content_hash: str
    storage_uri: str
    mime_type: str | None
    size_bytes: int | None
    processing_status: DocumentProcessingStatus
    created_at: datetime


class DocumentClassificationResponse(ApiModel):
    classification_id: str
    document_version_id: str
    role: DocumentRole
    confidence: float
    explanation: str
    evidence_chunk_ids: list[str]
    provider_name: str
    model_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    replayed: bool


class IngestDocumentResponse(ApiModel):
    document_id: str
    document_version_id: str
    content_hash: str
    storage_uri: str
    replayed: bool
    created_run_ids: list[str]
    reconciled_blocked_run_id: str | None
    classification: DocumentClassificationResponse | None = None


class RunStepResponse(ApiModel):
    step_id: str
    stage: RunStage
    status: RunStepStatus
    attempt: int
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    retry_operation_scope: str | None
    retry_operation_key: str | None
    retry_from_claim_generation: int | None
    created_at: datetime


class RunResponse(ApiModel):
    run_id: str
    pile_id: str
    run_type: RunType
    status: RunStatus
    trigger_document_version_id: str | None
    parent_run_id: str | None
    baseline_run_id: str | None
    blocked_on_run_id: str | None
    commit_required: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    steps: list[RunStepResponse]


class AdvanceRunRequest(ApiModel):
    source_rule_set_ids: list[str] | None = None
    source_rule_ids: list[str] | None = None


class AdvanceRunResponse(ApiModel):
    run_id: str
    stage: RunStage
    attempt: int
    step_status: RunStepStatus
    run_status: RunStatus
    outcome: str
    advanced: bool
    successor_attempt: int | None = None


class EvidenceResponse(ApiModel):
    evidence_id: str
    document_version_id: str
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str


class FactResponse(ApiModel):
    run_id: str
    fact_id: str
    relation: RunMembershipRelation
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus
    evidence: list[EvidenceResponse]


class FindingResponse(ApiModel):
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


class RunMetricResponse(ApiModel):
    metric_id: str
    run_id: str
    stage: RunStage
    attempt: int
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None
    created_at: datetime
