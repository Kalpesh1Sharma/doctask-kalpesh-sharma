"""Serialization helpers shared by MCP tools without duplicating use-case logic."""

from __future__ import annotations

from api.rest.schemas.core import (
    AdvanceRunResponse,
    DocumentClassificationResponse,
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
from api.rest.schemas.review import (
    FindingDecisionResponse,
    ReviewDecisionResponse,
    ReviewFindingResponse,
    RunReviewResponse,
    UpdateChangeItemResponse,
    UpdateChangeSetResponse,
    UpdateDecisionResponse,
    UpdateFactSnapshotResponse,
    UpdateItemReviewDecisionResponse,
    UpdateReviewDecisionResponse,
)


def pile_response(item: object) -> PileResponse:
    return PileResponse.model_validate(item.__dict__)


def rule_response(item: object) -> RuleResponse:
    return RuleResponse.model_validate(item.__dict__)


def rule_set_response(item: object) -> RuleSetResponse:
    payload = dict(item.__dict__)
    payload["rules"] = [rule_response(rule) for rule in payload["rules"]]
    return RuleSetResponse.model_validate(payload)


def document_response(item: object) -> DocumentResponse:
    return DocumentResponse.model_validate(item.__dict__)


def document_version_response(item: object) -> DocumentVersionResponse:
    return DocumentVersionResponse.model_validate(item.__dict__)


def classification_response(item: object) -> DocumentClassificationResponse:
    payload = dict(item.__dict__)
    payload["evidence_chunk_ids"] = list(payload["evidence_chunk_ids"])
    return DocumentClassificationResponse.model_validate(payload)


def evidence_response(item: object) -> EvidenceResponse:
    return EvidenceResponse.model_validate(item.__dict__)


def fact_response(item: object) -> FactResponse:
    payload = dict(item.__dict__)
    payload["evidence"] = [evidence_response(evidence) for evidence in payload["evidence"]]
    return FactResponse.model_validate(payload)


def finding_response(item: object) -> FindingResponse:
    payload = dict(item.__dict__)
    payload["evidence"] = [evidence_response(evidence) for evidence in payload["evidence"]]
    return FindingResponse.model_validate(payload)


def run_step_response(item: object) -> RunStepResponse:
    return RunStepResponse.model_validate(item.__dict__)


def run_response(item: object) -> RunResponse:
    payload = dict(item.__dict__)
    payload["steps"] = [run_step_response(step) for step in payload["steps"]]
    return RunResponse.model_validate(payload)


def ingest_response(item: object) -> IngestDocumentResponse:
    return IngestDocumentResponse(
        document_id=item.document_id,
        document_version_id=item.document_version_id,
        content_hash=item.content_hash,
        storage_uri=item.storage_uri,
        replayed=item.replayed,
        created_run_ids=list(item.created_run_ids),
        reconciled_blocked_run_id=item.reconciled_blocked_run_id,
        classification=(
            classification_response(item.classification)
            if getattr(item, "classification", None) is not None
            else None
        ),
    )


def advance_response(item: object) -> AdvanceRunResponse:
    return AdvanceRunResponse(
        run_id=item.run_id,
        stage=item.stage,
        attempt=item.step.attempt,
        step_status=item.step.status,
        run_status=item.run_status,
        outcome=item.outcome.value,
        advanced=item.advanced,
        successor_attempt=item.successor_attempt,
    )


def metric_response(item: object) -> RunMetricResponse:
    return RunMetricResponse.model_validate(item.__dict__)


def review_response(item: object) -> RunReviewResponse:
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
                evidence=[evidence_response(evidence) for evidence in finding.evidence],
                decision=decision,
            ),
        )

    update_change_set = None
    if item.update_change_set is not None:
        update_decision = (
            UpdateReviewDecisionResponse.model_validate(item.update_change_set.decision.__dict__)
            if item.update_change_set.decision is not None
            else None
        )
        update_items = []
        for update_item in getattr(item.update_change_set, "items", ()):
            item_decision = (
                UpdateItemReviewDecisionResponse.model_validate(
                    update_item.decision.__dict__,
                )
                if update_item.decision is not None
                else None
            )
            update_items.append(
                UpdateChangeItemResponse(
                    item_id=update_item.item_id,
                    fact_id=update_item.fact_id,
                    kind=update_item.kind,
                    payload=update_item.payload,
                    fact=UpdateFactSnapshotResponse.model_validate(
                        {
                            **update_item.fact.__dict__,
                            "evidence": [
                                evidence.__dict__ for evidence in update_item.fact.evidence
                            ],
                        },
                    ),
                    baseline_facts=[
                        UpdateFactSnapshotResponse.model_validate(
                            {
                                **fact.__dict__,
                                "evidence": [evidence.__dict__ for evidence in fact.evidence],
                            },
                        )
                        for fact in update_item.baseline_facts
                    ],
                    decision=item_decision,
                ),
            )
        update_change_set = UpdateChangeSetResponse(
            change_set_id=item.update_change_set.change_set_id,
            durable=item.update_change_set.durable,
            change_set_fingerprint=item.update_change_set.change_set_fingerprint,
            manifest=item.update_change_set.manifest,
            items=update_items,
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


def finding_decision_response(item: object) -> FindingDecisionResponse:
    decision = item.review_decision
    return FindingDecisionResponse(
        review_decision_id=decision.review_decision_id,
        finding_id=decision.finding_id,
        decision=decision.decision,
        actor_type=decision.actor_type,
        actor_id=decision.actor_id,
        idempotency_key=decision.idempotency_key,
        decided_at=decision.created_at,
        finding_status=item.finding.status,
        review_complete=item.review_complete,
        replayed=item.replayed,
    )


def update_decision_response(item: object) -> UpdateDecisionResponse:
    item_review = getattr(item, "item_review", None)
    if item_review is not None:
        decision = item_review.decision
        return UpdateDecisionResponse(
            change_set_id=item_review.change_set.change_set_id,
            change_set_fingerprint=item_review.change_set.change_set_fingerprint,
            fact_id=item_review.item.fact_id,
            item_kind=item_review.item.kind,
            decision=decision.decision,
            actor_type=decision.actor_type,
            actor_id=decision.actor_id,
            idempotency_key=decision.idempotency_key,
            decided_at=decision.decided_at,
            review_complete=item_review.review_complete,
            replayed=item_review.replayed,
        )
    assert item.review is not None
    decision = item.review.decision
    return UpdateDecisionResponse(
        change_set_id=item.review.change_set.change_set_id,
        change_set_fingerprint=item.review.change_set.change_set_fingerprint,
        fact_id=None,
        item_kind=None,
        decision=decision.decision,
        actor_type=decision.actor_type,
        actor_id=decision.actor_id,
        idempotency_key=decision.idempotency_key,
        decided_at=decision.decided_at,
        review_complete=True,
        replayed=item.review.replayed,
    )
