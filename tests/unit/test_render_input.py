from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from domain.fact import FactStatus
from domain.finding import FindingStatus, FindingType
from domain.render_input import (
    FactEvidenceReferences,
    FindingEvidenceReferences,
    FindingOriginRule,
    FindingOriginRuleSet,
    RenderInputDocument,
    RenderInputEvidenceSpan,
    RenderInputFact,
    RenderInputRule,
    RenderInputRuleSet,
    RenderInputSource,
    build_render_input_manifest,
    render_input_canonical_bytes,
    render_input_fingerprint,
)
from domain.review_state import (
    ReviewStateMembership,
    ReviewStateSnapshot,
    build_review_state_snapshot,
    review_state_fingerprint,
)
from domain.run_membership import RunMembershipRelation

RUN_ID = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
ORIGIN_RUN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DOCUMENT_VERSION_ID = "11111111-1111-1111-1111-111111111111"
DOCUMENT_ID = "22222222-2222-2222-2222-222222222222"
FACT_ID = "33333333-3333-3333-3333-333333333333"
FINDING_ID = "44444444-4444-4444-4444-444444444444"
EVIDENCE_ID = "55555555-5555-5555-5555-555555555555"
CHUNK_ID = "66666666-6666-6666-6666-666666666666"
RULE_SET_ID = "77777777-7777-7777-7777-777777777777"
RULE_ID = "88888888-8888-8888-8888-888888888888"


@dataclass(frozen=True)
class Scope:
    run_id: str


def review_state(
    *,
    finding_type: FindingType = FindingType.CONTRADICTION,
    origin_run_id: str = RUN_ID,
    rule_id: str | None = None,
) -> ReviewStateSnapshot:
    return build_review_state_snapshot(
        RUN_ID,
        [
            ReviewStateMembership(
                finding_id=FINDING_ID,
                relation=RunMembershipRelation.CREATED,
                origin_run_id=origin_run_id,
                finding_type=finding_type,
                rule_id=rule_id,
                title="Contradiction",
                description="Résumé ✓",
                severity="HIGH",
                status=FindingStatus.APPROVED,
            ),
        ],
    )


def source(*, include_evidence: bool = True) -> RenderInputSource:
    evidence = (
        RenderInputEvidenceSpan(
            evidence_id=EVIDENCE_ID,
            document_version_id=DOCUMENT_VERSION_ID,
            chunk_id=CHUNK_ID,
            page_number=1,
            start_offset=2,
            end_offset=5,
            source_text="exact source text",
            content_hash="sha256:evidence",
        ),
    ) if include_evidence else ()
    return RenderInputSource(
        documents=(
            RenderInputDocument(
                document_version_id=DOCUMENT_VERSION_ID,
                document_id=DOCUMENT_ID,
                version_number=1,
                content_hash="sha256:document",
            ),
        ),
        facts=(
            RenderInputFact(
                fact_id=FACT_ID,
                origin_run_id=RUN_ID,
                relation=RunMembershipRelation.CREATED,
                subject="vendor",
                predicate="notice_period",
                object_value={"days": [30, {"minimum": True}]},
                status=FactStatus.CONFIRMED,
            ),
        ),
        evidence_spans=evidence,
        fact_evidence=(FactEvidenceReferences(fact_id=FACT_ID, evidence_span_ids=(EVIDENCE_ID,) if include_evidence else ()),),
        finding_evidence=(FindingEvidenceReferences(finding_id=FINDING_ID, evidence_span_ids=(EVIDENCE_ID,) if include_evidence else ()),),
    )


def test_canonical_render_input_v1_is_order_invariant_and_pinned() -> None:
    manifest = build_render_input_manifest(RUN_ID, review_state(), source(), Scope(RUN_ID))
    assert render_input_canonical_bytes(manifest) == (
        b'{"format":"render-input-v1","review_state":{"format":"review-state-v1",'
        b'"memberships":[{"description":"R\xc3\xa9sum\xc3\xa9 \xe2\x9c\x93",'
        b'"finding_id":"44444444-4444-4444-4444-444444444444",'
        b'"finding_type":"CONTRADICTION",'
        b'"origin_run_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",'
        b'"relation":"CREATED","rule_id":null,"severity":"HIGH",'
        b'"status":"APPROVED","title":"Contradiction"}],'
        b'"run_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},'
        b'"run_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","source":{'
        b'"documents":[{"content_hash":"sha256:document",'
        b'"document_id":"22222222-2222-2222-2222-222222222222",'
        b'"document_version_id":"11111111-1111-1111-1111-111111111111",'
        b'"version_number":1}],'
        b'"evaluated_rule_sets":[],"evaluated_rules":[],"evidence_spans":[{'
        b'"chunk_id":"66666666-6666-6666-6666-666666666666",'
        b'"content_hash":"sha256:evidence",'
        b'"document_version_id":"11111111-1111-1111-1111-111111111111",'
        b'"end_offset":5,"evidence_id":"55555555-5555-5555-5555-555555555555",'
        b'"page_number":1,"source_text":"exact source text","start_offset":2}],'
        b'"fact_evidence":[{"evidence_span_ids":["55555555-5555-5555-5555-555555555555"],'
        b'"fact_id":"33333333-3333-3333-3333-333333333333"}],"facts":[{'
        b'"fact_id":"33333333-3333-3333-3333-333333333333",'
        b'"object_value":{"days":[30,{"minimum":true}]},'
        b'"origin_run_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",'
        b'"predicate":"notice_period","relation":"CREATED","status":"CONFIRMED",'
        b'"subject":"vendor"}],"finding_evidence":[{"evidence_span_ids":['
        b'"55555555-5555-5555-5555-555555555555"],'
        b'"finding_id":"44444444-4444-4444-4444-444444444444"}],'
        b'"finding_origin_rule_sets":[],"finding_origin_rules":[]}}'
    )
    assert render_input_fingerprint(manifest) == (
        "sha256:9f0218c9cb35a027328837f52d10386f4e2a02be9b2b54dacd6f486cdd65c228"
    )
    assert review_state_fingerprint(manifest.review_state) != render_input_fingerprint(manifest)


def test_valid_uppercase_ids_normalize_but_invalid_and_duplicate_ids_are_rejected() -> None:
    manifest = build_render_input_manifest(RUN_ID.upper(), review_state(), source(), Scope(RUN_ID.upper()))
    assert manifest.run_id == RUN_ID.lower()
    assert manifest.source.documents[0].document_version_id == DOCUMENT_VERSION_ID
    with pytest.raises(ValidationError):
        RenderInputDocument(
            document_version_id="invalid",
            document_id=DOCUMENT_ID,
            version_number=1,
            content_hash="hash",
        )
    with pytest.raises(ValidationError, match="unique"):
        RenderInputSource(documents=(
            source().documents[0],
            source().documents[0].model_copy(update={"document_version_id": DOCUMENT_VERSION_ID.upper()}),
        ))


def test_deep_json_is_immutable_and_semantic_changes_change_the_fingerprint() -> None:
    original = build_render_input_manifest(RUN_ID, review_state(), source(), Scope(RUN_ID))
    with pytest.raises(TypeError):
        original.source.facts[0].object_value["days"] = []  # type: ignore[index]
    with pytest.raises(TypeError):
        original.source.facts[0].object_value["days"][1]["minimum"] = False  # type: ignore[index]
    rule = RenderInputRule(
        source_rule_id=RULE_ID,
        source_rule_set_id=RULE_SET_ID,
        rule_key="notice",
        title="Notice",
        severity="HIGH",
        definition={"thresholds": [{"days": 30}]},
    )
    with pytest.raises(TypeError):
        rule.definition["thresholds"][0]["days"] = 31  # type: ignore[index]
    changed = build_render_input_manifest(
        RUN_ID,
        review_state(),
        source().model_copy(update={"documents": (
            source().documents[0].model_copy(update={"content_hash": "sha256:changed"}),
        )}),
        Scope(RUN_ID),
    )
    assert render_input_fingerprint(changed) != render_input_fingerprint(original)


def test_canonical_ordering_is_invariant_to_source_input_order() -> None:
    first_document = source(include_evidence=False).documents[0]
    second_document = first_document.model_copy(update={
        "document_version_id": "99999999-9999-9999-9999-999999999999",
        "document_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    })
    ordered = RenderInputSource(documents=(first_document, second_document))
    reversed_input = RenderInputSource(documents=(second_document, first_document))
    empty_review_state = build_review_state_snapshot(RUN_ID, [])

    ordered_manifest = build_render_input_manifest(RUN_ID, empty_review_state, ordered, Scope(RUN_ID))
    reversed_manifest = build_render_input_manifest(
        RUN_ID,
        empty_review_state,
        reversed_input,
        Scope(RUN_ID),
    )

    assert ordered.documents[0].document_version_id == DOCUMENT_VERSION_ID
    assert render_input_canonical_bytes(ordered_manifest) == render_input_canonical_bytes(reversed_manifest)


def test_builder_rejects_identity_reference_and_scope_inconsistencies() -> None:
    with pytest.raises(ValueError, match="same run"):
        build_render_input_manifest(RUN_ID, review_state(), source(), Scope(ORIGIN_RUN_ID))
    with pytest.raises(ValueError, match="document inputs"):
        bad = source().model_copy(update={"documents": ()})
        build_render_input_manifest(RUN_ID, review_state(), bad, Scope(RUN_ID))
    with pytest.raises(ValueError, match="fact evidence"):
        bad = source().model_copy(update={"fact_evidence": ()})
        build_render_input_manifest(RUN_ID, review_state(), bad, Scope(RUN_ID))
    with pytest.raises(ValueError, match="completed run rule scope"):
        build_render_input_manifest(RUN_ID, review_state(), source(), None)


def test_current_and_carried_rule_violations_require_exact_immutable_rule_context() -> None:
    current_review = review_state(finding_type=FindingType.RULE_VIOLATION, rule_id=RULE_ID)
    evaluated_source = source().model_copy(update={
        "evaluated_rule_sets": (RenderInputRuleSet(source_rule_set_id=RULE_SET_ID, name="Rules", version=1),),
        "evaluated_rules": (RenderInputRule(
            source_rule_id=RULE_ID,
            source_rule_set_id=RULE_SET_ID,
            rule_key="notice",
            title="Notice",
            severity="HIGH",
            definition={"days": 30},
        ),),
    })
    assert build_render_input_manifest(RUN_ID, current_review, evaluated_source, Scope(RUN_ID))

    carried_review = review_state(
        finding_type=FindingType.RULE_VIOLATION,
        origin_run_id=ORIGIN_RUN_ID,
        rule_id=RULE_ID,
    )
    origin_context = source().model_copy(update={
        "finding_origin_rule_sets": (FindingOriginRuleSet(
            origin_run_id=ORIGIN_RUN_ID,
            source_rule_set_id=RULE_SET_ID,
            name="Historical rules",
            description="v1",
            version=1,
        ),),
        "finding_origin_rules": (FindingOriginRule(
            origin_run_id=ORIGIN_RUN_ID,
            source_rule_id=RULE_ID,
            source_rule_set_id=RULE_SET_ID,
            rule_key="notice",
            title="Historical notice",
            severity="HIGH",
            definition={"days": 30},
        ),),
    })
    assert build_render_input_manifest(RUN_ID, carried_review, origin_context, Scope(RUN_ID))
    with pytest.raises(ValueError, match="origin rule snapshots"):
        build_render_input_manifest(
            RUN_ID,
            carried_review,
            origin_context.model_copy(update={"finding_origin_rules": ()}),
            Scope(RUN_ID),
        )


def test_contradictions_reject_origin_rule_context_and_empty_states_are_valid() -> None:
    empty_review = build_review_state_snapshot(RUN_ID, [])
    empty = build_render_input_manifest(RUN_ID, empty_review, RenderInputSource(), Scope(RUN_ID))
    assert empty.source.documents == ()
    bad = source().model_copy(update={
        "facts": (),
        "fact_evidence": (),
        "finding_origin_rule_sets": (FindingOriginRuleSet(
            origin_run_id=ORIGIN_RUN_ID,
            source_rule_set_id=RULE_SET_ID,
            name="Rules",
            version=1,
        ),),
    })
    with pytest.raises(ValueError, match="origin rule snapshots"):
        build_render_input_manifest(RUN_ID, review_state(), bad, Scope(RUN_ID))
