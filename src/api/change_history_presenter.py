"""Transport-neutral projection of durable UPDATE change history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

from api.rest.schemas.history import (
    ChangeHistoryCommitActorResponse,
    ChangeHistoryDocumentChangeResponse,
    ChangeHistoryEvidenceResponse,
    ChangeHistoryFactResponse,
    ChangeHistoryItemResponse,
    ChangeHistoryReviewDecisionResponse,
    ChangeHistoryRunResponse,
    ChangeHistorySourceResponse,
)
from domain.change_history import (
    ChangeHistoryCommitActor,
    ChangeHistoryDocumentChange,
    ChangeHistoryEvidence,
    ChangeHistoryFact,
    ChangeHistoryItem,
    ChangeHistoryReviewDecision,
    ChangeHistoryRun,
    ChangeHistorySource,
)


def _json_value(value: object) -> JsonValue:
    """Thaw immutable Fact JSON values before crossing the API boundary."""

    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return cast(JsonValue, value)


def _source(item: ChangeHistorySource) -> ChangeHistorySourceResponse:
    return ChangeHistorySourceResponse(
        pile_id=item.pile_id,
        document_id=item.document_id,
        logical_key=item.logical_key,
        document_version_id=item.document_version_id,
        version_number=item.version_number,
        content_hash=item.content_hash,
        mime_type=item.mime_type,
        role=item.role,
    )


def _review(item: ChangeHistoryReviewDecision) -> ChangeHistoryReviewDecisionResponse:
    return ChangeHistoryReviewDecisionResponse(
        decision_id=item.decision_id,
        decision=item.decision,
        actor_type=item.actor_type,
        actor_id=item.actor_id,
        decided_at=item.decided_at,
    )


def _commit_actor(item: ChangeHistoryCommitActor) -> ChangeHistoryCommitActorResponse:
    return ChangeHistoryCommitActorResponse(
        actor_type=item.actor_type,
        actor_id=item.actor_id,
    )


def _document_change(item: ChangeHistoryDocumentChange) -> ChangeHistoryDocumentChangeResponse:
    return ChangeHistoryDocumentChangeResponse(
        document_id=item.document_id,
        kind=item.kind,
        baseline_source=(
            _source(item.baseline_source)
            if item.baseline_source is not None
            else None
        ),
        target_source=_source(item.target_source),
    )


def _evidence(item: ChangeHistoryEvidence) -> ChangeHistoryEvidenceResponse:
    return ChangeHistoryEvidenceResponse(
        evidence_id=item.evidence_id,
        source=_source(item.source),
        chunk_id=item.chunk_id,
        page_number=item.page_number,
        start_offset=item.start_offset,
        end_offset=item.end_offset,
        source_text=item.source_text,
        content_hash=item.content_hash,
    )


def _fact(item: ChangeHistoryFact) -> ChangeHistoryFactResponse:
    return ChangeHistoryFactResponse(
        fact_id=item.fact_id,
        origin_run_id=item.origin_run_id,
        subject=item.subject,
        predicate=item.predicate,
        object_value=_json_value(item.object_value),
        status=item.status,
    )


def _change_item(item: ChangeHistoryItem) -> ChangeHistoryItemResponse:
    return ChangeHistoryItemResponse(
        change_item_id=item.change_item_id,
        kind=item.kind,
        outcome=item.outcome,
        fact=_fact(item.fact),
        review=_review(item.review),
        evidence=[_evidence(evidence) for evidence in item.evidence],
    )


def change_history_response(item: ChangeHistoryRun) -> ChangeHistoryRunResponse:
    """Project one validated domain history result into a stable external DTO."""

    return ChangeHistoryRunResponse(
        run_id=item.run_id,
        pile_id=item.pile_id,
        baseline_run_id=item.baseline_run_id,
        change_set_id=item.change_set_id,
        trigger_source=_source(item.trigger_source),
        committed_at=item.committed_at,
        output_id=item.output_id,
        output_content_hash=item.output_content_hash,
        commit_actor=(
            _commit_actor(item.commit_actor)
            if item.commit_actor is not None
            else None
        ),
        document_changes=[_document_change(change) for change in item.document_changes],
        items=[_change_item(change_item) for change_item in item.items],
        whole_update_review=(
            _review(item.whole_update_review)
            if item.whole_update_review is not None
            else None
        ),
    )
