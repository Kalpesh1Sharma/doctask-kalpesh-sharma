"""Reconstruct machine-readable change history from immutable committed state.

This service intentionally creates no second history table. Completed Runs,
change sets, item decisions, source versions, evidence, outputs, and audit
receipts are already durable/immutable facts. History is a validated projection
of those rows, so it cannot drift from the state that actually committed.
"""

from __future__ import annotations

from uuid import UUID

from psycopg import Connection
from psycopg_pool import ConnectionPool

from domain.change_history import (
    ChangeHistoryCommitActor,
    ChangeHistoryDocumentChange,
    ChangeHistoryEvidence,
    ChangeHistoryFact,
    ChangeHistoryItem,
    ChangeHistoryOutcome,
    ChangeHistoryReviewDecision,
    ChangeHistoryRun,
    ChangeHistorySource,
)
from domain.run import RunStatus, RunType
from domain.update_preparation import UpdateDocumentChangeKind
from domain.update_review import UpdateChangeItemKind, UpdateFactDeltaKind, UpdateReviewDecisionType
from repositories.audit_event_repository import AuditEventRepository
from repositories.document_classification_repository import DocumentClassificationRepository
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.output_repository import OutputRepository
from repositories.pile_repository import PileRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.update_change_item_repository import UpdateChangeItemRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from repositories.update_item_review_decision_repository import UpdateItemReviewDecisionRepository
from repositories.update_review_decision_repository import UpdateReviewDecisionRepository

_OUTPUT_TYPE = "FINAL_REPORT"
_COMMIT_EVENT_TYPE = "FINAL_OUTPUT_COMMITTED"


class ChangeHistoryApplicationError(Exception):
    pass


class InvalidChangeHistoryQueryError(ChangeHistoryApplicationError):
    pass


class ChangeHistoryPileNotFoundError(ChangeHistoryApplicationError):
    pass


class ChangeHistoryRunNotFoundError(ChangeHistoryApplicationError):
    pass


class ChangeHistoryIntegrityError(ChangeHistoryApplicationError):
    pass


class ChangeHistoryService:
    """Explain committed UPDATEs, including rejected proposed changes."""

    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def list_for_pile(self, pile_id: str, *, limit: int = 100) -> tuple[ChangeHistoryRun, ...]:
        normalized_pile_id = self._uuid(pile_id, "pile_id")
        bounded_limit = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if PileRepository(connection).get_by_id(normalized_pile_id) is None:
                raise ChangeHistoryPileNotFoundError(
                    f"pile {normalized_pile_id} does not exist",
                )
            runs = RunRepository(connection).list_completed_updates_for_pile(
                normalized_pile_id,
                limit=bounded_limit,
            )
            return tuple(self._project_run(connection, run) for run in runs)

    def get_for_run(self, run_id: str) -> ChangeHistoryRun:
        normalized_run_id = self._uuid(run_id, "run_id")
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_by_id(normalized_run_id)
            if run is None:
                raise ChangeHistoryRunNotFoundError(f"run {normalized_run_id} does not exist")
            if run.run_type is not RunType.UPDATE or run.status is not RunStatus.COMPLETED:
                raise InvalidChangeHistoryQueryError(
                    "change history is available only for completed UPDATE Runs",
                )
            return self._project_run(connection, run)

    def _project_run(self, connection: Connection, run: RunRecord) -> ChangeHistoryRun:
        if (
            run.run_type is not RunType.UPDATE
            or run.status is not RunStatus.COMPLETED
            or run.completed_at is None
            or run.baseline_run_id is None
            or run.trigger_document_version_id is None
            or run.blocked_on_run_id is not None
        ):
            raise ChangeHistoryIntegrityError(
                f"completed UPDATE Run {run.run_id} has invalid committed lineage",
            )

        change_set = UpdateChangeSetRepository(connection).get_by_run_id(run.run_id)
        if change_set is None:
            raise ChangeHistoryIntegrityError(
                f"completed UPDATE Run {run.run_id} has no durable change set",
            )
        if (
            change_set.baseline_run_id != run.baseline_run_id
            or change_set.trigger_document_version_id != run.trigger_document_version_id
        ):
            raise ChangeHistoryIntegrityError(
                f"change set for Run {run.run_id} disagrees with committed Run lineage",
            )
        # Parsing the immutable manifest verifies its durable fingerprint before
        # any history result is exposed.
        manifest = change_set.to_manifest()
        if (
            manifest.run_id != run.run_id
            or manifest.baseline_run_id != run.baseline_run_id
            or manifest.trigger_document_version_id != run.trigger_document_version_id
        ):
            raise ChangeHistoryIntegrityError(
                f"change-set manifest for Run {run.run_id} disagrees with durable lineage",
            )

        output = OutputRepository(connection).get_by_run_and_type(run.run_id, _OUTPUT_TYPE)
        if output is None:
            raise ChangeHistoryIntegrityError(
                f"completed UPDATE Run {run.run_id} has no final report output",
            )

        trigger_source = self._source_for_version(connection, run.trigger_document_version_id)
        if trigger_source is None or trigger_source.pile_id != run.pile_id:
            raise ChangeHistoryIntegrityError(
                f"UPDATE Run {run.run_id} trigger source cannot be resolved in its Pile",
            )

        document_changes: list[ChangeHistoryDocumentChange] = []
        trigger_change_found = False
        for change in manifest.document_delta:
            target = self._source_for_version(connection, change.target_document_version_id)
            if (
                target is None
                or target.pile_id != run.pile_id
                or target.document_id != change.document_id
                or target.version_number != change.target_version_number
                or target.content_hash != change.target_content_hash
            ):
                raise ChangeHistoryIntegrityError(
                    f"UPDATE Run {run.run_id} document delta cannot resolve target source",
                )
            baseline = None
            if change.baseline_document_version_id is not None:
                baseline = self._source_for_version(
                    connection,
                    change.baseline_document_version_id,
                )
                if (
                    baseline is None
                    or baseline.pile_id != run.pile_id
                    or baseline.document_id != change.document_id
                ):
                    raise ChangeHistoryIntegrityError(
                        f"UPDATE Run {run.run_id} document delta cannot resolve baseline source",
                    )
            if target.document_version_id == run.trigger_document_version_id:
                if change.kind is UpdateDocumentChangeKind.RETAINED:
                    raise ChangeHistoryIntegrityError(
                        f"UPDATE Run {run.run_id} trigger source is unexpectedly RETAINED",
                    )
                trigger_change_found = True
            document_changes.append(
                ChangeHistoryDocumentChange(
                    document_id=change.document_id,
                    kind=change.kind,
                    baseline_source=baseline,
                    target_source=target,
                ),
            )
        if not trigger_change_found:
            raise ChangeHistoryIntegrityError(
                f"UPDATE Run {run.run_id} document delta omits its trigger source",
            )

        items = UpdateChangeItemRepository(connection).list_for_change_set(change_set.change_set_id)
        material_delta = {
            delta.fact_id: delta.kind
            for delta in manifest.fact_delta
            if delta.kind
            in {
                UpdateFactDeltaKind.CREATED,
                UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE,
            }
        }
        if set(material_delta) != {item.fact_id for item in items}:
            raise ChangeHistoryIntegrityError(
                f"UPDATE Run {run.run_id} material items disagree with its change-set manifest",
            )
        for item in items:
            expected_kind = (
                UpdateChangeItemKind.FACT_CREATED
                if material_delta[item.fact_id] is UpdateFactDeltaKind.CREATED
                else UpdateChangeItemKind.FACT_REMOVED
            )
            if item.kind is not expected_kind:
                raise ChangeHistoryIntegrityError(
                    f"UPDATE Run {run.run_id} item kind disagrees with its change-set manifest",
                )

        item_decisions = UpdateItemReviewDecisionRepository(connection).list_for_change_set(
            change_set.change_set_id,
        )
        by_item_id = {decision.item_id: decision for decision in item_decisions}
        if len(by_item_id) != len(item_decisions):
            raise ChangeHistoryIntegrityError("duplicate UPDATE item review decisions detected")

        whole_review = UpdateReviewDecisionRepository(connection).get_by_change_set_id(
            change_set.change_set_id,
        )
        if items:
            if set(by_item_id) != {item.item_id for item in items}:
                raise ChangeHistoryIntegrityError(
                    f"completed UPDATE Run {run.run_id} lacks a decision for every material item",
                )
            if (
                whole_review is not None
                and whole_review.decision is UpdateReviewDecisionType.REJECTED
            ):
                raise ChangeHistoryIntegrityError(
                    f"completed UPDATE Run {run.run_id} carries a legacy whole-set rejection",
                )
            whole_projection = None
        else:
            if by_item_id:
                raise ChangeHistoryIntegrityError(
                    "zero-item UPDATE unexpectedly has item decisions",
                )
            if (
                whole_review is None
                or whole_review.decision is not UpdateReviewDecisionType.APPROVED
            ):
                raise ChangeHistoryIntegrityError(
                    f"completed zero-item UPDATE Run {run.run_id} lacks whole-set approval",
                )
            whole_projection = ChangeHistoryReviewDecision(
                decision_id=whole_review.update_review_decision_id,
                decision=whole_review.decision,
                actor_type=whole_review.actor_type,
                actor_id=whole_review.actor_id,
                decided_at=whole_review.decided_at,
            )

        projected_items: list[ChangeHistoryItem] = []
        fact_repository = FactRepository(connection)
        fact_evidence_repository = FactEvidenceRepository(connection)
        evidence_repository = EvidenceSpanRepository(connection)
        for item in items:
            decision = by_item_id[item.item_id]
            fact = fact_repository.get_by_id(item.fact_id)
            if fact is None:
                raise ChangeHistoryIntegrityError(
                    f"UPDATE history item {item.item_id} references missing Fact {item.fact_id}",
                )
            evidence: list[ChangeHistoryEvidence] = []
            for link in fact_evidence_repository.list_for_fact(item.fact_id):
                span = evidence_repository.get_by_id(link.evidence_span_id)
                if span is None:
                    raise ChangeHistoryIntegrityError(
                        f"Fact {item.fact_id} references missing evidence {link.evidence_span_id}",
                    )
                source = self._source_for_version(connection, span.document_version_id)
                if source is None or source.pile_id != run.pile_id:
                    raise ChangeHistoryIntegrityError(
                        "evidence "
                        f"{span.evidence_id} source version cannot be resolved in the Pile",
                    )
                evidence.append(
                    ChangeHistoryEvidence(
                        evidence_id=span.evidence_id,
                        source=source,
                        chunk_id=span.chunk_id,
                        page_number=span.page_number,
                        start_offset=span.start_offset,
                        end_offset=span.end_offset,
                        source_text=span.source_text,
                        content_hash=span.content_hash,
                    ),
                )
            if not evidence:
                raise ChangeHistoryIntegrityError(
                    f"material UPDATE Fact {item.fact_id} has no exact source evidence",
                )
            projected_items.append(
                ChangeHistoryItem(
                    change_item_id=item.item_id,
                    kind=item.kind,
                    outcome=(
                        ChangeHistoryOutcome.APPLIED
                        if decision.decision is UpdateReviewDecisionType.APPROVED
                        else ChangeHistoryOutcome.REJECTED
                    ),
                    fact=ChangeHistoryFact(
                        fact_id=fact.fact_id,
                        origin_run_id=fact.origin_run_id,
                        subject=fact.subject,
                        predicate=fact.predicate,
                        object_value=fact.object_value,
                        status=fact.status,
                    ),
                    review=ChangeHistoryReviewDecision(
                        decision_id=decision.update_item_review_decision_id,
                        decision=decision.decision,
                        actor_type=decision.actor_type,
                        actor_id=decision.actor_id,
                        decided_at=decision.decided_at,
                    ),
                    evidence=tuple(evidence),
                ),
            )

        commit_actor = self._commit_actor(connection, run.run_id, output.output_id)
        return ChangeHistoryRun(
            run_id=run.run_id,
            pile_id=run.pile_id,
            baseline_run_id=run.baseline_run_id,
            change_set_id=change_set.change_set_id,
            trigger_source=trigger_source,
            committed_at=run.completed_at,
            output_id=output.output_id,
            output_content_hash=output.content_hash,
            commit_actor=commit_actor,
            document_changes=tuple(document_changes),
            items=tuple(projected_items),
            whole_update_review=whole_projection,
        )

    @staticmethod
    def _commit_actor(
        connection: Connection,
        run_id: str,
        output_id: str,
    ) -> ChangeHistoryCommitActor | None:
        matching = [
            event
            for event in AuditEventRepository(connection).list_for_run(run_id)
            if event.event_type == _COMMIT_EVENT_TYPE
            and event.entity_type == "output"
            and event.entity_id == output_id
        ]
        if len(matching) > 1:
            raise ChangeHistoryIntegrityError(
                f"Run {run_id} has multiple commit audit receipts for output {output_id}",
            )
        if not matching:
            # Historical pre-cutover outputs may not have the Phase-6 receipt.
            # Absence is explicit rather than inventing an actor.
            return None
        return ChangeHistoryCommitActor(
            actor_type=matching[0].actor_type,
            actor_id=matching[0].actor_id,
        )

    @staticmethod
    def _source_for_version(
        connection: Connection,
        document_version_id: str,
    ) -> ChangeHistorySource | None:
        version = DocumentVersionRepository(connection).get_by_id(document_version_id)
        if version is None:
            return None
        document = DocumentRepository(connection).get_by_id(version.document_id)
        if document is None:
            return None
        classification = DocumentClassificationRepository(connection).get_by_document_version(
            document_version_id,
        )
        return ChangeHistorySource(
            pile_id=document.pile_id,
            document_id=document.document_id,
            logical_key=document.logical_key,
            document_version_id=version.document_version_id,
            version_number=version.version_number,
            content_hash=version.content_hash,
            mime_type=version.mime_type,
            role=classification.role if classification is not None else None,
        )

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            return str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidChangeHistoryQueryError(f"{field} must be a valid UUID") from error

    @staticmethod
    def _limit(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 200:
            raise InvalidChangeHistoryQueryError("limit must be between 1 and 200")
        return value
