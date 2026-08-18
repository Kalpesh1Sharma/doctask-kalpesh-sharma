"""Read-only contracts for explaining committed UPDATE history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import JsonValue

from domain.document_classification import DocumentRole
from domain.fact import FactStatus
from domain.update_preparation import UpdateDocumentChangeKind
from domain.update_review import UpdateChangeItemKind, UpdateReviewDecisionType


class ChangeHistoryOutcome(StrEnum):
    """Whether a proposed material UPDATE item changed the committed deliverable."""

    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ChangeHistorySource:
    pile_id: str
    document_id: str
    logical_key: str
    document_version_id: str
    version_number: int
    content_hash: str
    mime_type: str | None
    role: DocumentRole | None


@dataclass(frozen=True)
class ChangeHistoryDocumentChange:
    document_id: str
    kind: UpdateDocumentChangeKind
    baseline_source: ChangeHistorySource | None
    target_source: ChangeHistorySource


@dataclass(frozen=True)
class ChangeHistoryEvidence:
    evidence_id: str
    source: ChangeHistorySource
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str


@dataclass(frozen=True)
class ChangeHistoryFact:
    fact_id: str
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus


@dataclass(frozen=True)
class ChangeHistoryReviewDecision:
    decision_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    decided_at: datetime


@dataclass(frozen=True)
class ChangeHistoryCommitActor:
    actor_type: str
    actor_id: str


@dataclass(frozen=True)
class ChangeHistoryItem:
    change_item_id: str
    kind: UpdateChangeItemKind
    outcome: ChangeHistoryOutcome
    fact: ChangeHistoryFact
    review: ChangeHistoryReviewDecision
    evidence: tuple[ChangeHistoryEvidence, ...]


@dataclass(frozen=True)
class ChangeHistoryRun:
    run_id: str
    pile_id: str
    baseline_run_id: str
    change_set_id: str
    trigger_source: ChangeHistorySource
    committed_at: datetime
    output_id: str
    output_content_hash: str
    commit_actor: ChangeHistoryCommitActor | None
    document_changes: tuple[ChangeHistoryDocumentChange, ...]
    items: tuple[ChangeHistoryItem, ...]
    whole_update_review: ChangeHistoryReviewDecision | None

    @property
    def applied_items(self) -> tuple[ChangeHistoryItem, ...]:
        return tuple(item for item in self.items if item.outcome is ChangeHistoryOutcome.APPLIED)
