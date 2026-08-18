"""Resolve the Fact state that actually committed after item-level UPDATE review."""

from __future__ import annotations

from dataclasses import replace

from psycopg import Connection

from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_review import (
    UpdateChangeItemKind,
    UpdateFactDeltaKind,
    UpdateReviewDecisionType,
)
from repositories.run_fact_repository import RunFactReconciliationRecord, RunFactRepository
from repositories.run_repository import RunRepository
from repositories.update_change_item_repository import UpdateChangeItemRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from repositories.update_item_review_decision_repository import UpdateItemReviewDecisionRepository
from repositories.update_review_decision_repository import UpdateReviewDecisionRepository


class EffectiveRunFactStateIntegrityError(ValueError):
    """Committed UPDATE review state cannot reconstruct its effective Fact state."""


def list_effective_committed_facts(
    connection: Connection,
    run_id: str,
) -> tuple[RunFactReconciliationRecord, ...]:
    """Return the Fact state represented by a completed run's committed report.

    Durable ``run_facts`` preserve the model's proposed state for audit. Item
    review decisions are therefore applied as a projection when a later UPDATE
    needs the previous committed state: rejected CREATED Facts disappear and
    rejected REMOVED Facts are restored from the prior committed baseline.
    """

    return _list_effective_committed_facts(connection, run_id, frozenset())


def _list_effective_committed_facts(
    connection: Connection,
    run_id: str,
    ancestors: frozenset[str],
) -> tuple[RunFactReconciliationRecord, ...]:
    if run_id in ancestors:
        raise EffectiveRunFactStateIntegrityError("UPDATE baseline lineage contains a cycle")

    run = RunRepository(connection).get_by_id(run_id)
    if run is None or run.status is not RunStatus.COMPLETED:
        raise EffectiveRunFactStateIntegrityError(
            f"effective Fact state requires a completed Run {run_id}",
        )

    proposed = {
        record.fact_id: record
        for record in RunFactRepository(connection).list_reconciliation_input_for_run(run_id)
    }
    if run.run_type is not RunType.UPDATE:
        return tuple(proposed[fact_id] for fact_id in sorted(proposed))

    if run.baseline_run_id is None:
        raise EffectiveRunFactStateIntegrityError("completed UPDATE has no baseline Run")
    stored = UpdateChangeSetRepository(connection).get_by_run_id(run_id)
    if stored is None:
        raise EffectiveRunFactStateIntegrityError("completed UPDATE has no durable change set")
    manifest = stored.to_manifest()

    items = UpdateChangeItemRepository(connection).list_for_change_set(stored.change_set_id)
    if not items:
        decision = UpdateReviewDecisionRepository(connection).get_by_change_set_id(
            stored.change_set_id,
        )
        if decision is None or decision.decision is not UpdateReviewDecisionType.APPROVED:
            raise EffectiveRunFactStateIntegrityError(
                "completed zero-item UPDATE lacks an approved whole-change-set decision",
            )
        return tuple(proposed[fact_id] for fact_id in sorted(proposed))

    legacy_decision = UpdateReviewDecisionRepository(connection).get_by_change_set_id(
        stored.change_set_id,
    )
    if (
        legacy_decision is not None
        and legacy_decision.decision is UpdateReviewDecisionType.REJECTED
    ):
        raise EffectiveRunFactStateIntegrityError(
            "completed itemized UPDATE carries a legacy whole-set rejection",
        )

    decisions = {
        decision.item_id: decision
        for decision in UpdateItemReviewDecisionRepository(connection).list_for_change_set(
            stored.change_set_id,
        )
    }
    if len(decisions) != len(items):
        raise EffectiveRunFactStateIntegrityError(
            "completed itemized UPDATE lacks a decision for every material item",
        )

    material = {
        delta.fact_id: delta
        for delta in manifest.fact_delta
        if delta.kind in {
            UpdateFactDeltaKind.CREATED,
            UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE,
        }
    }
    if set(material) != {item.fact_id for item in items}:
        raise EffectiveRunFactStateIntegrityError(
            "durable UPDATE items disagree with the committed change-set manifest",
        )

    baseline = {
        record.fact_id: record
        for record in _list_effective_committed_facts(
            connection,
            run.baseline_run_id,
            ancestors | {run_id},
        )
    }
    effective = dict(proposed)
    for item in items:
        decision = decisions[item.item_id].decision
        if item.kind is UpdateChangeItemKind.FACT_CREATED:
            if item.fact_id not in proposed or item.fact_id in baseline:
                raise EffectiveRunFactStateIntegrityError(
                    f"created UPDATE item {item.fact_id} contradicts committed Fact state",
                )
            if decision is UpdateReviewDecisionType.REJECTED:
                effective.pop(item.fact_id, None)
            continue

        if item.fact_id in proposed or item.fact_id not in baseline:
            raise EffectiveRunFactStateIntegrityError(
                f"removed UPDATE item {item.fact_id} contradicts committed Fact state",
            )
        if decision is UpdateReviewDecisionType.REJECTED:
            effective[item.fact_id] = replace(
                baseline[item.fact_id],
                run_id=run_id,
                relation=RunMembershipRelation.CARRIED_FORWARD,
            )

    return tuple(effective[fact_id] for fact_id in sorted(effective))
