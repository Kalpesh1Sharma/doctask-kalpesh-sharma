from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

import pytest

from domain.fact import FactStatus
from domain.final_report import FinalReportInput
from domain.finding import FindingStatus, FindingType
from domain.render_input import (
    FactEvidenceReferences,
    FindingEvidenceReferences,
    FindingOriginRule,
    FindingOriginRuleSet,
    RenderInputDocument,
    RenderInputEvidenceSpan,
    RenderInputFact,
    RenderInputManifest,
    RenderInputRule,
    RenderInputRuleSet,
    RenderInputSource,
    build_render_input_manifest,
)
from domain.review_state import (
    ReviewStateMembership,
    ReviewStateSnapshot,
    build_review_state_snapshot,
)
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import UpdateDocumentChange, UpdateDocumentChangeKind
from domain.update_review import (
    UpdateChangeSetManifest,
    UpdateFactDelta,
    UpdateFactDeltaKind,
)
from services.final_report_renderer import (
    FinalReportRenderIntegrityError,
    final_report_content_hash,
    render_final_report,
    render_focused_update,
)

RUN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DOCUMENT_VERSION_ID = "11111111-1111-1111-1111-111111111111"
DOCUMENT_ID = "22222222-2222-2222-2222-222222222222"
FACT_1_ID = "33333333-3333-3333-3333-333333333333"
FACT_2_ID = "44444444-4444-4444-4444-444444444444"
FINDING_1_ID = "55555555-5555-5555-5555-555555555555"
FINDING_2_ID = "66666666-6666-6666-6666-666666666666"
EVIDENCE_1_ID = "77777777-7777-7777-7777-777777777777"
EVIDENCE_2_ID = "88888888-8888-8888-8888-888888888888"
EVIDENCE_3_ID = "99999999-9999-9999-9999-999999999999"
CHUNK_1_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CHUNK_2_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
CHUNK_3_ID = "cccccccc-dddd-eeee-ffff-000000000000"
RULE_ID = "dddddddd-eeee-ffff-0000-111111111111"
UPDATE_RUN_ID = "abababab-abab-abab-abab-abababababab"
TRIGGER_DOCUMENT_ID = "12121212-1212-1212-1212-121212121212"
TRIGGER_VERSION_ID = "13131313-1313-1313-1313-131313131313"


@dataclass(frozen=True)
class Scope:
    run_id: str


def _evidence(evidence_id: str, chunk_id: str, text: str, offset: int) -> RenderInputEvidenceSpan:
    return RenderInputEvidenceSpan(
        evidence_id=evidence_id,
        document_version_id=DOCUMENT_VERSION_ID,
        chunk_id=chunk_id,
        page_number=1,
        start_offset=offset,
        end_offset=offset + len(text),
        source_text=text,
        content_hash=f"sha256:{evidence_id}",
    )


def _review_state() -> ReviewStateSnapshot:
    return build_review_state_snapshot(
        RUN_ID,
        [
            ReviewStateMembership(
                finding_id=FINDING_1_ID,
                relation=RunMembershipRelation.CREATED,
                origin_run_id=RUN_ID,
                finding_type=FindingType.RULE_VIOLATION,
                rule_id=RULE_ID,
                title="Missing insurance",
                description="Insurance evidence is missing.",
                severity="HIGH",
                status=FindingStatus.APPROVED,
            ),
            ReviewStateMembership(
                finding_id=FINDING_2_ID,
                relation=RunMembershipRelation.CARRIED_FORWARD,
                origin_run_id=RUN_ID,
                finding_type=FindingType.CONTRADICTION,
                rule_id=None,
                title="Address mismatch",
                description="Two documents disagree on the address.",
                severity="MEDIUM",
                status=FindingStatus.REJECTED,
            ),
        ],
    )


def _source(*, reverse: bool = False) -> RenderInputSource:
    documents = (
        RenderInputDocument(
            document_version_id=DOCUMENT_VERSION_ID,
            document_id=DOCUMENT_ID,
            version_number=1,
            content_hash="sha256:document",
        ),
    )
    facts = (
        RenderInputFact(
            fact_id=FACT_1_ID,
            origin_run_id=RUN_ID,
            relation=RunMembershipRelation.CREATED,
            subject="loan",
            predicate="amount",
            object_value="USD 250000",
            status=FactStatus.CONFIRMED,
        ),
        RenderInputFact(
            fact_id=FACT_2_ID,
            origin_run_id=RUN_ID,
            relation=RunMembershipRelation.CARRIED_FORWARD,
            subject="borrower",
            predicate="name",
            object_value="Ada Borrower",
            status=FactStatus.DISPUTED,
        ),
    )
    evidence = (
        _evidence(EVIDENCE_1_ID, CHUNK_1_ID, "$250,000", 10),
        _evidence(EVIDENCE_2_ID, CHUNK_2_ID, "Ada Borrower", 30),
        _evidence(EVIDENCE_3_ID, CHUNK_3_ID, "Insurance: none", 50),
    )
    fact_evidence = (
        FactEvidenceReferences(fact_id=FACT_1_ID, evidence_span_ids=(EVIDENCE_1_ID,)),
        FactEvidenceReferences(fact_id=FACT_2_ID, evidence_span_ids=(EVIDENCE_2_ID,)),
    )
    finding_evidence = (
        FindingEvidenceReferences(finding_id=FINDING_1_ID, evidence_span_ids=(EVIDENCE_3_ID,)),
        FindingEvidenceReferences(finding_id=FINDING_2_ID, evidence_span_ids=(EVIDENCE_2_ID,)),
    )
    if reverse:
        facts = tuple(reversed(facts))
        evidence = tuple(reversed(evidence))
        fact_evidence = tuple(reversed(fact_evidence))
        finding_evidence = tuple(reversed(finding_evidence))
    return RenderInputSource(
        documents=documents,
        facts=facts,
        evidence_spans=evidence,
        fact_evidence=fact_evidence,
        finding_evidence=finding_evidence,
        evaluated_rule_sets=(
            RenderInputRuleSet(
                source_rule_set_id="eeeeeeee-ffff-0000-1111-222222222222",
                name="Closing rules",
                version=1,
            ),
        ),
        evaluated_rules=(
            RenderInputRule(
                source_rule_id=RULE_ID,
                source_rule_set_id="eeeeeeee-ffff-0000-1111-222222222222",
                rule_key="insurance-required",
                title="Insurance required",
                severity="HIGH",
                definition={"required": True},
            ),
        ),
    )


def _final_report_input(*, reverse: bool = False) -> FinalReportInput:
    manifest = build_render_input_manifest(
        RUN_ID,
        _review_state(),
        _source(reverse=reverse),
        Scope(RUN_ID),
    )
    return FinalReportInput(render_input=manifest)


def _line_for(rendered: bytes, prefix: str) -> str:
    return next(line for line in rendered.decode("utf-8").splitlines() if line.startswith(prefix))


def test_each_fact_line_contains_exact_durable_fact_evidence_ids() -> None:
    rendered = render_final_report(_final_report_input())

    confirmed = _line_for(rendered, f'- fact_id: "{FACT_1_ID}"')
    disputed = _line_for(rendered, f'- fact_id: "{FACT_2_ID}"')

    assert f'evidence_span_ids: ["{EVIDENCE_1_ID}"]' in confirmed
    assert EVIDENCE_2_ID not in confirmed
    assert f'evidence_span_ids: ["{EVIDENCE_2_ID}"]' in disputed
    assert EVIDENCE_1_ID not in disputed


def test_fact_object_value_supports_frozen_nested_json_objects() -> None:
    source = _source()
    nested_fact = RenderInputFact(
        fact_id=FACT_1_ID,
        origin_run_id=RUN_ID,
        relation=RunMembershipRelation.CREATED,
        subject="loan",
        predicate="terms",
        object_value={"window": {"days": 30}, "flags": [True, None]},
        status=FactStatus.CONFIRMED,
    )
    source = source.model_copy(update={"facts": (nested_fact, source.facts[1])})
    manifest = build_render_input_manifest(RUN_ID, _review_state(), source, Scope(RUN_ID))

    rendered = render_final_report(FinalReportInput(render_input=manifest))

    confirmed = _line_for(rendered, f'- fact_id: "{FACT_1_ID}"')
    assert 'object: {"flags":[true,null],"window":{"days":30}}' in confirmed


def test_each_finding_line_contains_rule_and_exact_durable_finding_evidence_ids() -> None:
    rendered = render_final_report(_final_report_input())

    approved = _line_for(rendered, f'- finding_id: "{FINDING_1_ID}"')
    rejected = _line_for(rendered, f'- finding_id: "{FINDING_2_ID}"')

    assert f'rule_id: "{RULE_ID}"' in approved
    assert f'evidence_span_ids: ["{EVIDENCE_3_ID}"]' in approved
    assert EVIDENCE_2_ID not in approved
    assert "rule_id: null" in rejected
    assert f'evidence_span_ids: ["{EVIDENCE_2_ID}"]' in rejected
    assert EVIDENCE_3_ID not in rejected


def test_claims_with_different_citations_remain_distinguishable_in_artifact() -> None:
    rendered = render_final_report(_final_report_input())

    fact_1 = _line_for(rendered, f'- fact_id: "{FACT_1_ID}"')
    fact_2 = _line_for(rendered, f'- fact_id: "{FACT_2_ID}"')
    finding_1 = _line_for(rendered, f'- finding_id: "{FINDING_1_ID}"')

    assert EVIDENCE_1_ID in fact_1 and EVIDENCE_2_ID not in fact_1 and EVIDENCE_3_ID not in fact_1
    assert EVIDENCE_2_ID in fact_2 and EVIDENCE_1_ID not in fact_2 and EVIDENCE_3_ID not in fact_2
    assert EVIDENCE_3_ID in finding_1 and EVIDENCE_1_ID not in finding_1 and EVIDENCE_2_ID not in finding_1


def test_source_insertion_order_does_not_change_rendered_bytes() -> None:
    assert render_final_report(_final_report_input()) == render_final_report(
        _final_report_input(reverse=True),
    )


def test_same_final_report_input_is_byte_identical_and_hash_matches_exact_bytes() -> None:
    value = _final_report_input()

    first = render_final_report(value)
    second = render_final_report(value)

    assert first == second
    assert final_report_content_hash(value) == f"sha256:{sha256(first).hexdigest()}"


def test_renderer_fails_closed_when_claim_references_missing_frozen_evidence() -> None:
    source = _source()
    missing_evidence_source = source.model_copy(update={
        "evidence_spans": tuple(
            evidence for evidence in source.evidence_spans if evidence.evidence_id != EVIDENCE_1_ID
        ),
    })
    # Construct the manifest directly to prove the renderer protects its own artifact boundary;
    # the normal build_render_input_manifest() path rejects this inconsistency even earlier.
    manifest = RenderInputManifest(
        run_id=RUN_ID,
        review_state=_review_state(),
        source=missing_evidence_source,
    )

    with pytest.raises(FinalReportRenderIntegrityError, match="references evidence absent"):
        render_final_report(FinalReportInput(render_input=manifest))



def _focused_update_input() -> FinalReportInput:
    baseline_source = _source()
    source = baseline_source.model_copy(
        update={
            "documents": baseline_source.documents
            + (
                RenderInputDocument(
                    document_version_id=TRIGGER_VERSION_ID,
                    document_id=TRIGGER_DOCUMENT_ID,
                    version_number=1,
                    content_hash="sha256:trigger-document",
                ),
            ),
            "facts": tuple(
                fact.model_copy(update={"relation": RunMembershipRelation.CARRIED_FORWARD})
                for fact in baseline_source.facts
            ),
            "finding_origin_rule_sets": tuple(
                FindingOriginRuleSet(
                    origin_run_id=RUN_ID,
                    **rule_set.model_dump(mode="json"),
                )
                for rule_set in baseline_source.evaluated_rule_sets
            ),
            "finding_origin_rules": tuple(
                FindingOriginRule(
                    origin_run_id=RUN_ID,
                    **rule.model_dump(mode="json"),
                )
                for rule in baseline_source.evaluated_rules
            ),
        },
    )
    baseline_review = _review_state()
    review = build_review_state_snapshot(
        UPDATE_RUN_ID,
        tuple(
            membership.model_copy(
                update={
                    "relation": RunMembershipRelation.CARRIED_FORWARD,
                    "origin_run_id": membership.origin_run_id,
                },
            )
            for membership in baseline_review.memberships
        ),
    )
    manifest = build_render_input_manifest(
        UPDATE_RUN_ID,
        review,
        source,
        Scope(UPDATE_RUN_ID),
    )
    change_set = UpdateChangeSetManifest(
        run_id=UPDATE_RUN_ID,
        baseline_run_id=RUN_ID,
        trigger_document_version_id=TRIGGER_VERSION_ID,
        preparation_fingerprint="sha256:" + "a" * 64,
        document_delta=(
            UpdateDocumentChange(
                document_id=DOCUMENT_ID,
                kind=UpdateDocumentChangeKind.RETAINED,
                baseline_document_version_id=DOCUMENT_VERSION_ID,
                target_document_version_id=DOCUMENT_VERSION_ID,
                target_version_number=1,
                target_content_hash="sha256:document",
            ),
            UpdateDocumentChange(
                document_id=TRIGGER_DOCUMENT_ID,
                kind=UpdateDocumentChangeKind.ADDED,
                baseline_document_version_id=None,
                target_document_version_id=TRIGGER_VERSION_ID,
                target_version_number=1,
                target_content_hash="sha256:trigger-document",
            ),
        ),
        fact_delta=(
            UpdateFactDelta(fact_id=FACT_1_ID, kind=UpdateFactDeltaKind.CARRIED_FORWARD),
            UpdateFactDelta(fact_id=FACT_2_ID, kind=UpdateFactDeltaKind.CARRIED_FORWARD),
        ),
        review_state_fingerprint="sha256:" + "b" * 64,
        render_input_fingerprint="sha256:" + "c" * 64,
    )
    return FinalReportInput(render_input=manifest, update_change_set=change_set)


def test_focused_update_copies_unaffected_baseline_bytes_and_matches_canonical_report() -> None:
    baseline = render_final_report(_final_report_input())
    baseline_hash = f"sha256:{sha256(baseline).hexdigest()}"
    update_input = _focused_update_input()

    updated, proof = render_focused_update(update_input, baseline, baseline_hash)
    replayed, replayed_proof = render_focused_update(update_input, baseline, baseline_hash)

    assert updated == replayed
    assert proof == replayed_proof
    assert updated == render_final_report(update_input)
    assert proof.baseline_content_hash == baseline_hash
    part_ids = {part.part_id for part in proof.preserved_parts}
    assert f"document:{DOCUMENT_ID}" in part_ids
    assert f"fact:{FACT_1_ID}:identity" in part_ids
    assert f"fact:{FACT_1_ID}:claim-body" in part_ids
    assert f"fact:{FACT_2_ID}" in part_ids
    assert f"finding:{FINDING_1_ID}" in part_ids
    assert f"finding:{FINDING_2_ID}" in part_ids
    assert f"evidence:{EVIDENCE_1_ID}" in part_ids
    assert f"evidence:{EVIDENCE_2_ID}" in part_ids
    assert f"evidence:{EVIDENCE_3_ID}" in part_ids
    assert proof.preserved_bytes > 0
    for part in proof.preserved_parts:
        baseline_part = baseline[part.baseline_start : part.baseline_end]
        updated_part = updated[part.updated_start : part.updated_end]
        assert updated_part == baseline_part
        assert part.content_hash == f"sha256:{sha256(baseline_part).hexdigest()}"

    baseline_fact = _line_for(baseline, f'- fact_id: "{FACT_1_ID}"')
    updated_fact = _line_for(updated, f'- fact_id: "{FACT_1_ID}"')
    assert 'provenance: "CREATED"' in baseline_fact
    assert 'provenance: "CARRIED_FORWARD"' in updated_fact
    assert baseline_fact.split("; subject: ", 1)[1] == updated_fact.split("; subject: ", 1)[1]


def test_focused_update_fails_closed_when_a_carried_claim_changed_in_baseline() -> None:
    baseline = render_final_report(_final_report_input())
    corrupted = baseline.replace(b'object: "USD 250000"', b'object: "USD 999999"')
    assert corrupted != baseline

    with pytest.raises(FinalReportRenderIntegrityError, match="changed outside"):
        render_focused_update(
            _focused_update_input(),
            corrupted,
            f"sha256:{sha256(corrupted).hexdigest()}",
        )


def test_focused_update_rejects_baseline_hash_mismatch() -> None:
    baseline = render_final_report(_final_report_input())

    with pytest.raises(FinalReportRenderIntegrityError, match="hash does not match"):
        render_focused_update(
            _focused_update_input(),
            baseline,
            "sha256:" + "0" * 64,
        )
