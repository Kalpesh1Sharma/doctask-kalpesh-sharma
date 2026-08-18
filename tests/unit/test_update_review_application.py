from __future__ import annotations

import pytest

from domain.fact import FactStatus
from domain.final_report import FinalReportInput
from domain.render_input import (
    FactEvidenceReferences,
    RenderInputDocument,
    RenderInputEvidenceSpan,
    RenderInputFact,
    RenderInputManifest,
    RenderInputSource,
)
from domain.review_state import build_review_state_snapshot
from domain.run_membership import RunMembershipRelation
from domain.update_review import (
    UpdateChangeItemKind,
    UpdateChangeSetManifest,
    UpdateFactDelta,
    UpdateFactDeltaKind,
    UpdateReviewDecisionType,
)
from domain.update_review_application import (
    UpdateItemDecisionSnapshot,
    UpdateReviewApplicationIntegrityError,
    apply_update_item_decisions,
)
from services.final_report_renderer import render_final_report

BASELINE_RUN_ID = "11111111-1111-4111-8111-111111111111"
UPDATE_RUN_ID = "22222222-2222-4222-8222-222222222222"
OLD_FACT_ID = "33333333-3333-4333-8333-333333333333"
NEW_FACT_ID = "44444444-4444-4444-8444-444444444444"
OLD_DOCUMENT_ID = "55555555-5555-4555-8555-555555555555"
OLD_VERSION_ID = "66666666-6666-4666-8666-666666666666"
NEW_DOCUMENT_ID = "77777777-7777-4777-8777-777777777777"
NEW_VERSION_ID = "88888888-8888-4888-8888-888888888888"
OLD_EVIDENCE_ID = "99999999-9999-4999-8999-999999999999"
NEW_EVIDENCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OLD_CHUNK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NEW_CHUNK_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _fact(
    fact_id: str,
    run_id: str,
    relation: RunMembershipRelation,
    predicate: str,
) -> RenderInputFact:
    return RenderInputFact(
        fact_id=fact_id,
        origin_run_id=run_id,
        relation=relation,
        subject="loan",
        predicate=predicate,
        object_value={"value": predicate},
        status=FactStatus.CONFIRMED,
    )


def _evidence(
    evidence_id: str,
    version_id: str,
    chunk_id: str,
    text: str,
) -> RenderInputEvidenceSpan:
    return RenderInputEvidenceSpan(
        evidence_id=evidence_id,
        document_version_id=version_id,
        chunk_id=chunk_id,
        start_offset=0,
        end_offset=len(text),
        source_text=text,
        content_hash="sha256:" + "a" * 64,
    )


def _manifests() -> tuple[RenderInputManifest, RenderInputManifest, UpdateChangeSetManifest]:
    baseline = RenderInputManifest(
        run_id=BASELINE_RUN_ID,
        review_state=build_review_state_snapshot(BASELINE_RUN_ID, ()),
        source=RenderInputSource(
            documents=(
                RenderInputDocument(
                    document_version_id=OLD_VERSION_ID,
                    document_id=OLD_DOCUMENT_ID,
                    version_number=1,
                    content_hash="sha256:" + "1" * 64,
                ),
            ),
            facts=(
                _fact(
                    OLD_FACT_ID,
                    BASELINE_RUN_ID,
                    RunMembershipRelation.CREATED,
                    "interest_rate",
                ),
            ),
            evidence_spans=(
                _evidence(OLD_EVIDENCE_ID, OLD_VERSION_ID, OLD_CHUNK_ID, "Rate 6.25%"),
            ),
            fact_evidence=(
                FactEvidenceReferences(
                    fact_id=OLD_FACT_ID,
                    evidence_span_ids=(OLD_EVIDENCE_ID,),
                ),
            ),
        ),
    )
    current = RenderInputManifest(
        run_id=UPDATE_RUN_ID,
        review_state=build_review_state_snapshot(UPDATE_RUN_ID, ()),
        source=RenderInputSource(
            documents=(
                RenderInputDocument(
                    document_version_id=NEW_VERSION_ID,
                    document_id=NEW_DOCUMENT_ID,
                    version_number=1,
                    content_hash="sha256:" + "2" * 64,
                ),
            ),
            facts=(
                _fact(
                    NEW_FACT_ID,
                    UPDATE_RUN_ID,
                    RunMembershipRelation.CREATED,
                    "payment_amount",
                ),
            ),
            evidence_spans=(
                _evidence(NEW_EVIDENCE_ID, NEW_VERSION_ID, NEW_CHUNK_ID, "Payment 1500"),
            ),
            fact_evidence=(
                FactEvidenceReferences(
                    fact_id=NEW_FACT_ID,
                    evidence_span_ids=(NEW_EVIDENCE_ID,),
                ),
            ),
        ),
    )
    change_set = UpdateChangeSetManifest(
        run_id=UPDATE_RUN_ID,
        baseline_run_id=BASELINE_RUN_ID,
        trigger_document_version_id=NEW_VERSION_ID,
        preparation_fingerprint="sha256:" + "3" * 64,
        document_delta=(),
        fact_delta=(
            UpdateFactDelta(fact_id=NEW_FACT_ID, kind=UpdateFactDeltaKind.CREATED),
            UpdateFactDelta(
                fact_id=OLD_FACT_ID,
                kind=UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE,
            ),
        ),
        review_state_fingerprint="sha256:" + "4" * 64,
        render_input_fingerprint="sha256:" + "5" * 64,
    )
    return current, baseline, change_set


def test_mixed_item_decisions_apply_only_approved_changes_and_restore_rejected_removal() -> None:
    current, baseline, change_set = _manifests()

    applied = apply_update_item_decisions(
        current,
        baseline,
        change_set,
        (
            UpdateItemDecisionSnapshot(
                fact_id=NEW_FACT_ID,
                kind=UpdateChangeItemKind.FACT_CREATED,
                decision=UpdateReviewDecisionType.APPROVED,
            ),
            UpdateItemDecisionSnapshot(
                fact_id=OLD_FACT_ID,
                kind=UpdateChangeItemKind.FACT_REMOVED,
                decision=UpdateReviewDecisionType.REJECTED,
            ),
        ),
    )

    facts = {fact.fact_id: fact for fact in applied.source.facts}
    assert set(facts) == {NEW_FACT_ID, OLD_FACT_ID}
    assert facts[NEW_FACT_ID].relation is RunMembershipRelation.CREATED
    assert facts[OLD_FACT_ID].relation is RunMembershipRelation.CARRIED_FORWARD
    references = {
        item.fact_id: item.evidence_span_ids for item in applied.source.fact_evidence
    }
    assert references[NEW_FACT_ID] == (NEW_EVIDENCE_ID,)
    assert references[OLD_FACT_ID] == (OLD_EVIDENCE_ID,)
    assert {item.evidence_id for item in applied.source.evidence_spans} == {
        NEW_EVIDENCE_ID,
        OLD_EVIDENCE_ID,
    }


def test_rejected_created_fact_is_removed_with_its_orphaned_evidence() -> None:
    current, baseline, change_set = _manifests()

    applied = apply_update_item_decisions(
        current,
        baseline,
        change_set,
        (
            UpdateItemDecisionSnapshot(
                fact_id=NEW_FACT_ID,
                kind=UpdateChangeItemKind.FACT_CREATED,
                decision=UpdateReviewDecisionType.REJECTED,
            ),
            UpdateItemDecisionSnapshot(
                fact_id=OLD_FACT_ID,
                kind=UpdateChangeItemKind.FACT_REMOVED,
                decision=UpdateReviewDecisionType.APPROVED,
            ),
        ),
    )

    assert applied.source.facts == ()
    assert applied.source.fact_evidence == ()
    assert applied.source.evidence_spans == ()



def test_final_report_lists_only_applied_material_fact_changes() -> None:
    current, baseline, change_set = _manifests()
    applied = apply_update_item_decisions(
        current,
        baseline,
        change_set,
        (
            UpdateItemDecisionSnapshot(
                fact_id=NEW_FACT_ID,
                kind=UpdateChangeItemKind.FACT_CREATED,
                decision=UpdateReviewDecisionType.REJECTED,
            ),
            UpdateItemDecisionSnapshot(
                fact_id=OLD_FACT_ID,
                kind=UpdateChangeItemKind.FACT_REMOVED,
                decision=UpdateReviewDecisionType.APPROVED,
            ),
        ),
    )

    report = render_final_report(
        FinalReportInput(render_input=applied, update_change_set=change_set),
    ).decode("utf-8")

    assert NEW_FACT_ID not in report
    assert f'"fact_id":"{OLD_FACT_ID}","kind":"REMOVED_FROM_CURRENT_STATE"' in report


def test_item_decisions_must_cover_exact_material_delta() -> None:
    current, baseline, change_set = _manifests()

    with pytest.raises(UpdateReviewApplicationIntegrityError, match="cover every material"):
        apply_update_item_decisions(
            current,
            baseline,
            change_set,
            (
                UpdateItemDecisionSnapshot(
                    fact_id=NEW_FACT_ID,
                    kind=UpdateChangeItemKind.FACT_CREATED,
                    decision=UpdateReviewDecisionType.APPROVED,
                ),
            ),
        )
