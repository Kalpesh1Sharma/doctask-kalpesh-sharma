"""Read-only review and final-report projections for external interfaces."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import Connection
from psycopg_pool import ConnectionPool
from pydantic import JsonValue

from api.application.build_update_change_set_service import (
    UpdateChangeSetApplicationError,
    build_current_update_change_set_manifest,
)
from api.application.effective_run_fact_state import (
    EffectiveRunFactStateIntegrityError,
    list_effective_committed_facts,
)
from api.application.external_query_service import EvidenceProjection
from domain.fact import FactStatus
from domain.fact_semantics import (
    canonical_fact_concept_with_evidence,
    canonical_fact_display_value,
)
from domain.finding import FindingStatus, FindingType
from domain.review_decision import ReviewDecisionType
from domain.run import RunStatus, RunType
from domain.run_membership import RunMembershipRelation
from domain.update_review import (
    UpdateChangeItemKind,
    UpdateChangeSetInputIntegrityError,
    UpdateChangeSetIntegrityError,
    UpdateFactDeltaKind,
    UpdateReviewDecisionType,
    update_change_set_fingerprint,
)
from repositories.audit_event_repository import AuditEventRepository
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.fact_evidence_repository import FactEvidenceRepository
from repositories.fact_repository import FactRepository
from repositories.finding_evidence_repository import FindingEvidenceRepository
from repositories.output_repository import OutputRepository
from repositories.review_decision_repository import ReviewDecisionRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.update_change_item_repository import UpdateChangeItemRepository
from repositories.update_change_set_repository import UpdateChangeSetRepository
from repositories.update_item_review_decision_repository import UpdateItemReviewDecisionRepository
from repositories.update_review_decision_repository import UpdateReviewDecisionRepository
from services.local_artifact_storage import ArtifactIntegrityError, LocalArtifactStorage

_OUTPUT_TYPE = "FINAL_REPORT"
_AUDIT_ENTITY_TYPE = "output"
_AUDIT_EVENT_TYPE = "FINAL_OUTPUT_COMMITTED"
_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ExternalReviewQueryError(Exception):
    pass


class InvalidExternalReviewQueryError(ExternalReviewQueryError):
    pass


class ExternalReviewRunNotFoundError(ExternalReviewQueryError):
    pass


class ExternalReviewIntegrityError(ExternalReviewQueryError):
    pass


class FinalReportNotFoundError(ExternalReviewQueryError):
    pass


class FinalReportIntegrityError(ExternalReviewQueryError):
    pass


@dataclass(frozen=True)
class ReviewDecisionProjection:
    review_decision_id: str
    finding_id: str
    decision: ReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime


@dataclass(frozen=True)
class ReviewFindingProjection:
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
    evidence: tuple[EvidenceProjection, ...]
    decision: ReviewDecisionProjection | None


@dataclass(frozen=True)
class UpdateReviewDecisionProjection:
    update_review_decision_id: str
    change_set_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime


@dataclass(frozen=True)
class UpdateItemReviewDecisionProjection:
    update_item_review_decision_id: str
    item_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    decided_at: datetime


@dataclass(frozen=True)
class UpdateFactEvidenceProjection:
    evidence_id: str
    document_version_id: str
    logical_key: str
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str


@dataclass(frozen=True)
class UpdateFactSnapshotProjection:
    fact_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    display_value: JsonValue
    status: FactStatus
    concept: str
    evidence: tuple[UpdateFactEvidenceProjection, ...]


@dataclass(frozen=True)
class UpdateChangeItemProjection:
    item_id: str | None
    fact_id: str
    kind: UpdateChangeItemKind
    payload: JsonValue
    fact: UpdateFactSnapshotProjection
    baseline_facts: tuple[UpdateFactSnapshotProjection, ...]
    decision: UpdateItemReviewDecisionProjection | None


@dataclass(frozen=True)
class UpdateChangeSetProjection:
    change_set_id: str | None
    durable: bool
    change_set_fingerprint: str
    manifest: JsonValue
    items: tuple[UpdateChangeItemProjection, ...]
    decision: UpdateReviewDecisionProjection | None


@dataclass(frozen=True)
class RunReviewProjection:
    run_id: str
    run_type: RunType
    run_status: RunStatus
    findings: tuple[ReviewFindingProjection, ...]
    finding_review_complete: bool
    update_change_set: UpdateChangeSetProjection | None
    review_complete: bool
    commit_ready: bool


@dataclass(frozen=True)
class FinalReportProjection:
    output_id: str
    run_id: str
    output_type: str
    storage_uri: str
    content_hash: str
    created_at: datetime
    render_input_fingerprint: str
    focused_update: JsonValue | None


class ExternalReviewQueryService:
    def __init__(
        self,
        connection_pool: ConnectionPool,
        artifact_storage: LocalArtifactStorage,
    ) -> None:
        self._connection_pool = connection_pool
        self._artifact_storage = artifact_storage

    def get_review(self, run_id: str) -> RunReviewProjection:
        normalized = self._uuid(run_id, "run_id")
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_by_id(normalized)
            if run is None:
                raise ExternalReviewRunNotFoundError(f"run {normalized} does not exist")
            findings = self._findings(connection, run)
            finding_review_complete = all(
                item.status is not FindingStatus.PENDING for item in findings
            )
            update_change_set = self._update_change_set(
                connection,
                run,
                finding_review_complete,
            )

        update_decision = update_change_set.decision if update_change_set is not None else None
        update_items = update_change_set.items if update_change_set is not None else ()
        item_review_complete = bool(update_items) and all(
            item.decision is not None for item in update_items
        )
        legacy_itemized_rejection = (
            bool(update_items)
            and update_decision is not None
            and update_decision.decision is UpdateReviewDecisionType.REJECTED
        )
        update_review_complete = (
            run.run_type is not RunType.UPDATE
            or item_review_complete
            or (not update_items and update_decision is not None)
        )
        update_commit_allowed = (
            run.run_type is not RunType.UPDATE
            or (item_review_complete and not legacy_itemized_rejection)
            or (
                not update_items
                and update_decision is not None
                and update_decision.decision is UpdateReviewDecisionType.APPROVED
            )
        )
        review_complete = finding_review_complete and update_review_complete
        commit_ready = (
            run.status is RunStatus.REVIEW_REQUIRED
            and finding_review_complete
            and update_commit_allowed
        )
        return RunReviewProjection(
            run_id=run.run_id,
            run_type=run.run_type,
            run_status=run.status,
            findings=findings,
            finding_review_complete=finding_review_complete,
            update_change_set=update_change_set,
            review_complete=review_complete,
            commit_ready=commit_ready,
        )

    def get_report(self, run_id: str) -> FinalReportProjection:
        normalized = self._uuid(run_id, "run_id")
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_by_id(normalized)
            if run is None:
                raise ExternalReviewRunNotFoundError(f"run {normalized} does not exist")
            projection = self._report_projection(connection, run)
        try:
            self._artifact_storage.verify(
                projection.storage_uri,
                projection.content_hash,
            )
        except (ArtifactIntegrityError, OSError) as error:
            raise FinalReportIntegrityError(
                "committed final report artifact failed integrity verification",
            ) from error
        return projection

    def read_report_content(self, run_id: str) -> tuple[FinalReportProjection, bytes]:
        projection = self.get_report(run_id)
        try:
            content = self._artifact_storage.read_bytes(
                projection.storage_uri,
                projection.content_hash,
            )
        except (ArtifactIntegrityError, OSError) as error:
            raise FinalReportIntegrityError(
                "committed final report artifact failed integrity verification",
            ) from error
        return projection, content

    def _findings(
        self,
        connection: Connection,
        run: RunRecord,
    ) -> tuple[ReviewFindingProjection, ...]:
        records = RunFindingRepository(connection).list_review_state_for_run(run.run_id)
        evidence_links = FindingEvidenceRepository(connection)
        evidence_repository = EvidenceSpanRepository(connection)
        decision_repository = ReviewDecisionRepository(connection)
        result: list[ReviewFindingProjection] = []
        for record in records:
            evidence: list[EvidenceProjection] = []
            for link in evidence_links.list_for_finding(record.finding_id):
                span = evidence_repository.get_by_id(link.evidence_span_id)
                if span is None:
                    raise ExternalReviewIntegrityError(
                        f"finding {record.finding_id} references missing evidence",
                    )
                evidence.append(
                    EvidenceProjection(
                        evidence_id=span.evidence_id,
                        document_version_id=span.document_version_id,
                        chunk_id=span.chunk_id,
                        page_number=span.page_number,
                        start_offset=span.start_offset,
                        end_offset=span.end_offset,
                        source_text=span.source_text,
                        content_hash=span.content_hash,
                    ),
                )

            decisions = decision_repository.list_for_finding(record.finding_id)
            if len(decisions) > 1:
                raise ExternalReviewIntegrityError(
                    f"finding {record.finding_id} has multiple review decisions",
                )
            decision = None
            if decisions:
                item = decisions[0]
                decision = ReviewDecisionProjection(
                    review_decision_id=item.review_decision_id,
                    finding_id=item.finding_id,
                    decision=item.decision,
                    actor_type=item.actor_type,
                    actor_id=item.actor_id,
                    idempotency_key=item.idempotency_key,
                    decided_at=item.created_at,
                )
                if record.status.value != item.decision.value:
                    raise ExternalReviewIntegrityError(
                        f"finding {record.finding_id} status disagrees with its review decision",
                    )
            elif record.status is FindingStatus.PENDING:
                decision = None

            result.append(
                ReviewFindingProjection(
                    run_id=record.run_id,
                    finding_id=record.finding_id,
                    relation=record.relation,
                    origin_run_id=record.origin_run_id,
                    finding_type=record.finding_type,
                    rule_id=record.rule_id,
                    title=record.title,
                    description=record.description,
                    severity=record.severity,
                    status=record.status,
                    evidence=tuple(evidence),
                    decision=decision,
                ),
            )
        return tuple(result)

    def _update_change_set(
        self,
        connection: Connection,
        run: RunRecord,
        finding_review_complete: bool,
    ) -> UpdateChangeSetProjection | None:
        repository = UpdateChangeSetRepository(connection)
        stored = repository.get_by_run_id(run.run_id)
        if run.run_type is not RunType.UPDATE:
            if stored is not None:
                raise ExternalReviewIntegrityError(
                    "non-UPDATE Run unexpectedly has an UPDATE change set",
                )
            return None

        if stored is not None:
            try:
                manifest = stored.to_manifest()
            except UpdateChangeSetIntegrityError as error:
                raise ExternalReviewIntegrityError(
                    "stored UPDATE change set failed integrity verification",
                ) from error
            decision_record = UpdateReviewDecisionRepository(connection).get_by_change_set_id(
                stored.change_set_id,
            )
            decision = (
                UpdateReviewDecisionProjection(
                    update_review_decision_id=decision_record.update_review_decision_id,
                    change_set_id=decision_record.change_set_id,
                    decision=decision_record.decision,
                    actor_type=decision_record.actor_type,
                    actor_id=decision_record.actor_id,
                    idempotency_key=decision_record.idempotency_key,
                    decided_at=decision_record.decided_at,
                )
                if decision_record is not None
                else None
            )
            item_decisions = {
                record.item_id: record
                for record in UpdateItemReviewDecisionRepository(connection).list_for_change_set(
                    stored.change_set_id,
                )
            }
            baseline_by_concept = self._baseline_facts_by_concept(connection, run)
            items = []
            for item in UpdateChangeItemRepository(connection).list_for_change_set(
                stored.change_set_id,
            ):
                item_decision_record = item_decisions.get(item.item_id)
                item_decision = (
                    UpdateItemReviewDecisionProjection(
                        update_item_review_decision_id=(
                            item_decision_record.update_item_review_decision_id
                        ),
                        item_id=item_decision_record.item_id,
                        decision=item_decision_record.decision,
                        actor_type=item_decision_record.actor_type,
                        actor_id=item_decision_record.actor_id,
                        idempotency_key=item_decision_record.idempotency_key,
                        decided_at=item_decision_record.decided_at,
                    )
                    if item_decision_record is not None
                    else None
                )
                fact = self._fact_snapshot(connection, item.fact_id)
                items.append(
                    UpdateChangeItemProjection(
                        item_id=item.item_id,
                        fact_id=item.fact_id,
                        kind=item.kind,
                        payload=item.payload,
                        fact=fact,
                        baseline_facts=tuple(
                            baseline
                            for baseline in baseline_by_concept.get(fact.concept, ())
                            if baseline.fact_id != fact.fact_id
                        ),
                        decision=item_decision,
                    ),
                )
            return UpdateChangeSetProjection(
                change_set_id=stored.change_set_id,
                durable=True,
                change_set_fingerprint=stored.change_set_fingerprint,
                manifest=manifest.model_dump(mode="json"),
                items=tuple(items),
                decision=decision,
            )

        if run.status is RunStatus.COMPLETED:
            raise ExternalReviewIntegrityError(
                "completed UPDATE Run has no durable change set",
            )
        if not finding_review_complete or run.status is not RunStatus.REVIEW_REQUIRED:
            return None

        try:
            manifest = build_current_update_change_set_manifest(connection, run)
        except (UpdateChangeSetApplicationError, UpdateChangeSetInputIntegrityError) as error:
            raise ExternalReviewIntegrityError(
                "UPDATE review preview cannot be derived from durable state",
            ) from error
        baseline_by_concept = self._baseline_facts_by_concept(connection, run)
        preview_items: list[UpdateChangeItemProjection] = []
        for delta in manifest.fact_delta:
            if delta.kind not in {
                UpdateFactDeltaKind.CREATED,
                UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE,
            }:
                continue
            fact = self._fact_snapshot(connection, delta.fact_id)
            preview_items.append(
                UpdateChangeItemProjection(
                    item_id=None,
                    fact_id=delta.fact_id,
                    kind=(
                        UpdateChangeItemKind.FACT_CREATED
                        if delta.kind is UpdateFactDeltaKind.CREATED
                        else UpdateChangeItemKind.FACT_REMOVED
                    ),
                    payload=delta.model_dump(mode="json"),
                    fact=fact,
                    baseline_facts=tuple(
                        baseline
                        for baseline in baseline_by_concept.get(fact.concept, ())
                        if baseline.fact_id != fact.fact_id
                    ),
                    decision=None,
                ),
            )
        return UpdateChangeSetProjection(
            change_set_id=None,
            durable=False,
            change_set_fingerprint=update_change_set_fingerprint(manifest),
            manifest=manifest.model_dump(mode="json"),
            items=tuple(preview_items),
            decision=None,
        )

    def _baseline_facts_by_concept(
        self,
        connection: Connection,
        run: RunRecord,
    ) -> dict[str, tuple[UpdateFactSnapshotProjection, ...]]:
        if run.run_type is not RunType.UPDATE or run.baseline_run_id is None:
            return {}
        try:
            records = list_effective_committed_facts(connection, run.baseline_run_id)
        except EffectiveRunFactStateIntegrityError as error:
            raise ExternalReviewIntegrityError(
                "UPDATE review cannot reconstruct the committed baseline Fact state",
            ) from error

        grouped: dict[str, list[UpdateFactSnapshotProjection]] = {}
        for record in records:
            snapshot = self._fact_snapshot(connection, record.fact_id)
            grouped.setdefault(snapshot.concept, []).append(snapshot)
        return {
            concept: tuple(sorted(items, key=lambda item: item.fact_id))
            for concept, items in grouped.items()
        }

    @staticmethod
    def _fact_snapshot(
        connection: Connection,
        fact_id: str,
    ) -> UpdateFactSnapshotProjection:
        fact = FactRepository(connection).get_by_id(fact_id)
        if fact is None:
            raise ExternalReviewIntegrityError(
                f"UPDATE review item references missing Fact {fact_id}",
            )

        evidence_repository = EvidenceSpanRepository(connection)
        version_repository = DocumentVersionRepository(connection)
        document_repository = DocumentRepository(connection)
        evidence: list[UpdateFactEvidenceProjection] = []
        for link in FactEvidenceRepository(connection).list_for_fact(fact_id):
            span = evidence_repository.get_by_id(link.evidence_span_id)
            if span is None:
                raise ExternalReviewIntegrityError(
                    f"Fact {fact_id} references missing evidence {link.evidence_span_id}",
                )
            version = version_repository.get_by_id(span.document_version_id)
            if version is None:
                raise ExternalReviewIntegrityError(
                    f"evidence {span.evidence_id} references a missing document version",
                )
            document = document_repository.get_by_id(version.document_id)
            if document is None:
                raise ExternalReviewIntegrityError(
                    f"document version {version.document_version_id} references a missing document",
                )
            evidence.append(
                UpdateFactEvidenceProjection(
                    evidence_id=span.evidence_id,
                    document_version_id=span.document_version_id,
                    logical_key=document.logical_key,
                    chunk_id=span.chunk_id,
                    page_number=span.page_number,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                    source_text=span.source_text,
                    content_hash=span.content_hash,
                ),
            )

        source_texts = tuple(item.source_text for item in evidence)
        return UpdateFactSnapshotProjection(
            fact_id=fact.fact_id,
            subject=fact.subject,
            predicate=fact.predicate,
            object_value=fact.object_value,
            display_value=canonical_fact_display_value(
                fact.subject,
                fact.predicate,
                fact.object_value,
                source_texts,
            ),
            status=fact.status,
            concept=canonical_fact_concept_with_evidence(
                fact.subject,
                fact.predicate,
                source_texts,
            ),
            evidence=tuple(evidence),
        )

    def _report_projection(
        self,
        connection: Connection,
        run: RunRecord,
    ) -> FinalReportProjection:
        output = OutputRepository(connection).get_by_run_and_type(run.run_id, _OUTPUT_TYPE)
        if output is None:
            if run.status is RunStatus.COMPLETED:
                raise FinalReportIntegrityError(
                    "completed Run has no canonical FINAL_REPORT output",
                )
            raise FinalReportNotFoundError(f"run {run.run_id} has no committed final report")
        if run.status is not RunStatus.COMPLETED:
            raise FinalReportIntegrityError(
                "FINAL_REPORT output exists for a Run that is not COMPLETED",
            )

        bindings = [
            event
            for event in AuditEventRepository(connection).list_for_run(run.run_id)
            if event.entity_type == _AUDIT_ENTITY_TYPE
            and event.entity_id == output.output_id
            and event.event_type == _AUDIT_EVENT_TYPE
        ]
        if len(bindings) != 1:
            raise FinalReportIntegrityError(
                "FINAL_REPORT does not have exactly one finalization audit binding",
            )
        payload = bindings[0].payload
        if not isinstance(payload, dict):
            raise FinalReportIntegrityError("FINAL_REPORT audit binding payload is malformed")
        expected = {
            "run_id": run.run_id,
            "output_type": _OUTPUT_TYPE,
            "artifact_content_hash": output.content_hash,
            "storage_uri": output.storage_uri,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise FinalReportIntegrityError("FINAL_REPORT audit binding disagrees with output")
        render_fingerprint = payload.get("render_input_fingerprint")
        if not isinstance(render_fingerprint, str) or _FINGERPRINT_PATTERN.fullmatch(
            render_fingerprint,
        ) is None:
            raise FinalReportIntegrityError(
                "FINAL_REPORT audit binding has an invalid render fingerprint",
            )

        focused_update = payload.get("focused_update")
        if run.run_type is RunType.UPDATE:
            if not isinstance(focused_update, dict):
                raise FinalReportIntegrityError(
                    "UPDATE FINAL_REPORT audit binding lacks focused-update proof",
                )
        elif focused_update is not None:
            raise FinalReportIntegrityError(
                "non-UPDATE FINAL_REPORT unexpectedly carries focused-update proof",
            )

        return FinalReportProjection(
            output_id=output.output_id,
            run_id=run.run_id,
            output_type=output.output_type,
            storage_uri=output.storage_uri,
            content_hash=output.content_hash,
            created_at=output.created_at,
            render_input_fingerprint=render_fingerprint,
            focused_update=focused_update,
        )

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            return str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidExternalReviewQueryError(
                f"{field} must be a valid UUID",
            ) from error
