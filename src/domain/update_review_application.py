from __future__ import annotations

from dataclasses import dataclass

from domain.render_input import (
    FactEvidenceReferences,
    RenderInputManifest,
    RenderInputSource,
)
from domain.run_membership import RunMembershipRelation
from domain.update_review import (
    UpdateChangeItemKind,
    UpdateChangeSetManifest,
    UpdateFactDeltaKind,
    UpdateReviewDecisionType,
)


class UpdateReviewApplicationIntegrityError(ValueError):
    """Item decisions cannot be applied to the frozen UPDATE proposal safely."""


@dataclass(frozen=True)
class UpdateItemDecisionSnapshot:
    fact_id: str
    kind: UpdateChangeItemKind
    decision: UpdateReviewDecisionType


def apply_update_item_decisions(
    current: RenderInputManifest,
    baseline: RenderInputManifest,
    change_set: UpdateChangeSetManifest,
    decisions: tuple[UpdateItemDecisionSnapshot, ...],
) -> RenderInputManifest:
    """Return the deterministic deliverable state after mixed UPDATE decisions.

    CREATED Fact rejection removes the proposed claim. REMOVED Fact rejection
    restores the exact baseline claim as CARRIED_FORWARD together with its
    baseline evidence. Historical evidence may therefore point to the baseline
    source version that originally grounded the preserved claim; the current
    run's source-version list remains the actual current input scope.
    """

    if current.run_id != change_set.run_id:
        raise UpdateReviewApplicationIntegrityError("current render input is not bound to change set")
    if baseline.run_id != change_set.baseline_run_id:
        raise UpdateReviewApplicationIntegrityError("baseline render input is not bound to change set")

    reviewable = {
        delta.fact_id: delta
        for delta in change_set.fact_delta
        if delta.kind in {UpdateFactDeltaKind.CREATED, UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE}
    }
    if len(reviewable) != sum(
        delta.kind in {UpdateFactDeltaKind.CREATED, UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE}
        for delta in change_set.fact_delta
    ):
        raise UpdateReviewApplicationIntegrityError("reviewable UPDATE Fact delta is not unique")

    by_fact = {decision.fact_id: decision for decision in decisions}
    if len(by_fact) != len(decisions) or set(by_fact) != set(reviewable):
        raise UpdateReviewApplicationIntegrityError(
            "item decisions must cover every material UPDATE Fact delta exactly once",
        )

    current_facts = {fact.fact_id: fact for fact in current.source.facts}
    baseline_facts = {fact.fact_id: fact for fact in baseline.source.facts}
    fact_evidence = {
        reference.fact_id: reference
        for reference in current.source.fact_evidence
    }
    baseline_fact_evidence = {
        reference.fact_id: reference
        for reference in baseline.source.fact_evidence
    }
    evidence = {item.evidence_id: item for item in current.source.evidence_spans}
    baseline_evidence = {item.evidence_id: item for item in baseline.source.evidence_spans}

    for fact_id, decision in by_fact.items():
        delta = reviewable[fact_id]
        expected_kind = (
            UpdateChangeItemKind.FACT_CREATED
            if delta.kind is UpdateFactDeltaKind.CREATED
            else UpdateChangeItemKind.FACT_REMOVED
        )
        if decision.kind is not expected_kind:
            raise UpdateReviewApplicationIntegrityError(
                f"decision kind disagrees with Fact delta {fact_id}",
            )

        if delta.kind is UpdateFactDeltaKind.CREATED:
            if fact_id not in current_facts or fact_id in baseline_facts:
                raise UpdateReviewApplicationIntegrityError(
                    f"created Fact {fact_id} does not match current/baseline state",
                )
            if decision.decision is UpdateReviewDecisionType.REJECTED:
                current_facts.pop(fact_id)
                fact_evidence.pop(fact_id, None)
            continue

        if fact_id in current_facts or fact_id not in baseline_facts:
            raise UpdateReviewApplicationIntegrityError(
                f"removed Fact {fact_id} does not match current/baseline state",
            )
        if decision.decision is UpdateReviewDecisionType.REJECTED:
            baseline_fact = baseline_facts[fact_id]
            current_facts[fact_id] = baseline_fact.model_copy(
                update={"relation": RunMembershipRelation.CARRIED_FORWARD},
            )
            reference = baseline_fact_evidence.get(fact_id)
            if reference is None or not reference.evidence_span_ids:
                raise UpdateReviewApplicationIntegrityError(
                    f"rejected removal Fact {fact_id} lacks baseline evidence",
                )
            fact_evidence[fact_id] = FactEvidenceReferences(
                fact_id=fact_id,
                evidence_span_ids=reference.evidence_span_ids,
            )
            for evidence_id in reference.evidence_span_ids:
                baseline_item = baseline_evidence.get(evidence_id)
                if baseline_item is None:
                    raise UpdateReviewApplicationIntegrityError(
                        f"rejected removal Fact {fact_id} references missing baseline evidence",
                    )
                evidence[evidence_id] = baseline_item

    referenced = {
        evidence_id
        for reference in (*fact_evidence.values(), *current.source.finding_evidence)
        for evidence_id in reference.evidence_span_ids
    }
    missing = referenced.difference(evidence)
    if missing:
        raise UpdateReviewApplicationIntegrityError(
            f"decision-applied render input references missing evidence: {tuple(sorted(missing))}",
        )

    source = RenderInputSource(
        documents=current.source.documents,
        facts=tuple(current_facts.values()),
        evidence_spans=tuple(evidence[evidence_id] for evidence_id in referenced),
        fact_evidence=tuple(fact_evidence.values()),
        finding_evidence=current.source.finding_evidence,
        evaluated_rule_sets=current.source.evaluated_rule_sets,
        evaluated_rules=current.source.evaluated_rules,
        finding_origin_rule_sets=current.source.finding_origin_rule_sets,
        finding_origin_rules=current.source.finding_origin_rules,
    )
    return RenderInputManifest(
        run_id=current.run_id,
        review_state=current.review_state,
        source=source,
    )
