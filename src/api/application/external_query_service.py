"""Read-only projections used by external interfaces.

These projections deliberately contain no mutation logic. PostgreSQL remains
source of truth and all state transitions stay in their existing use cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import JsonValue

from domain.document_version import DocumentProcessingStatus
from domain.fact import FactStatus
from domain.finding import FindingStatus, FindingType
from domain.run import RunStage, RunStatus, RunStepStatus, RunType
from domain.run_membership import RunMembershipRelation
from repositories.document_repository import DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.evidence_span_repository import EvidenceSpanRepository
from repositories.finding_evidence_repository import FindingEvidenceRepository
from repositories.pile_repository import PileRepository
from repositories.rule_set_repository import RuleSetRepository
from repositories.run_fact_repository import RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_metric_repository import RunMetricRepository
from repositories.run_repository import RunRepository
from repositories.run_step_repository import RunStepRepository


class ExternalQueryApplicationError(Exception):
    pass


class InvalidExternalQueryError(ExternalQueryApplicationError):
    pass


class PileNotFoundError(ExternalQueryApplicationError):
    pass


class DocumentNotFoundError(ExternalQueryApplicationError):
    pass


class RuleSetNotFoundExternalError(ExternalQueryApplicationError):
    pass


class RunNotFoundExternalError(ExternalQueryApplicationError):
    pass


@dataclass(frozen=True)
class PileProjection:
    pile_id: str
    name: str
    description: str | None
    watch_enabled: bool
    watch_location: str | None
    latest_run_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DocumentProjection:
    document_id: str
    pile_id: str
    logical_key: str
    current_version_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DocumentVersionProjection:
    document_version_id: str
    document_id: str
    version_number: int
    content_hash: str
    storage_uri: str
    mime_type: str | None
    size_bytes: int | None
    processing_status: DocumentProcessingStatus
    created_at: datetime


@dataclass(frozen=True)
class RuleProjection:
    rule_id: str
    rule_set_id: str
    rule_key: str
    title: str
    description: str | None
    severity: str
    definition: JsonValue
    created_at: datetime


@dataclass(frozen=True)
class RuleSetProjection:
    rule_set_id: str
    pile_id: str
    name: str
    description: str | None
    version: int
    created_at: datetime
    rules: tuple[RuleProjection, ...]


@dataclass(frozen=True)
class RunStepProjection:
    step_id: str
    stage: RunStage
    status: RunStepStatus
    attempt: int
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    retry_operation_scope: str | None
    retry_operation_key: str | None
    retry_from_claim_generation: int | None
    created_at: datetime


@dataclass(frozen=True)
class RunProjection:
    run_id: str
    pile_id: str
    run_type: RunType
    status: RunStatus
    trigger_document_version_id: str | None
    parent_run_id: str | None
    baseline_run_id: str | None
    blocked_on_run_id: str | None
    commit_required: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    steps: tuple[RunStepProjection, ...]


@dataclass(frozen=True)
class EvidenceProjection:
    evidence_id: str
    document_version_id: str
    chunk_id: str
    page_number: int | None
    start_offset: int | None
    end_offset: int | None
    source_text: str
    content_hash: str


@dataclass(frozen=True)
class FactProjection:
    run_id: str
    fact_id: str
    relation: RunMembershipRelation
    origin_run_id: str
    subject: str
    predicate: str
    object_value: JsonValue
    status: FactStatus
    evidence: tuple[EvidenceProjection, ...]


@dataclass(frozen=True)
class FindingProjection:
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


@dataclass(frozen=True)
class RunMetricProjection:
    metric_id: str
    run_id: str
    stage: RunStage
    attempt: int
    duration_ms: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None
    created_at: datetime


class ExternalQueryService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def get_pile(self, pile_id: str) -> PileProjection:
        normalized = self._uuid(pile_id, "pile_id")
        with self._connection_pool.connection() as connection:
            item = PileRepository(connection).get_by_id(normalized)
        if item is None:
            raise PileNotFoundError(f"pile {normalized} does not exist")
        return PileProjection(**item.__dict__)

    def list_documents(self, pile_id: str, *, limit: int = 100) -> tuple[DocumentProjection, ...]:
        normalized = self._uuid(pile_id, "pile_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if PileRepository(connection).get_by_id(normalized) is None:
                raise PileNotFoundError(f"pile {normalized} does not exist")
            items = DocumentRepository(connection).list_for_pile(normalized, limit=bounded)
        return tuple(DocumentProjection(**item.__dict__) for item in items)

    def get_document(self, document_id: str) -> DocumentProjection:
        normalized = self._uuid(document_id, "document_id")
        with self._connection_pool.connection() as connection:
            item = DocumentRepository(connection).get_by_id(normalized)
        if item is None:
            raise DocumentNotFoundError(f"document {normalized} does not exist")
        return DocumentProjection(**item.__dict__)

    def list_document_versions(
        self,
        document_id: str,
        *,
        limit: int = 100,
    ) -> tuple[DocumentVersionProjection, ...]:
        normalized = self._uuid(document_id, "document_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if DocumentRepository(connection).get_by_id(normalized) is None:
                raise DocumentNotFoundError(f"document {normalized} does not exist")
            items = DocumentVersionRepository(connection).list_for_document(normalized)[:bounded]
        return tuple(DocumentVersionProjection(**item.__dict__) for item in items)

    def list_rule_sets(self, pile_id: str, *, limit: int = 100) -> tuple[RuleSetProjection, ...]:
        normalized = self._uuid(pile_id, "pile_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if PileRepository(connection).get_by_id(normalized) is None:
                raise PileNotFoundError(f"pile {normalized} does not exist")
            repository = RuleSetRepository(connection)
            items = repository.list_rule_sets_for_pile(normalized, limit=bounded)
            return tuple(self._rule_set_projection(repository, item) for item in items)

    def get_rule_set(self, rule_set_id: str) -> RuleSetProjection:
        normalized = self._uuid(rule_set_id, "rule_set_id")
        with self._connection_pool.connection() as connection:
            repository = RuleSetRepository(connection)
            item = repository.get_rule_set(normalized)
            if item is None:
                raise RuleSetNotFoundExternalError(f"rule set {normalized} does not exist")
            return self._rule_set_projection(repository, item)

    def list_runs(self, pile_id: str, *, limit: int = 100) -> tuple[RunProjection, ...]:
        normalized = self._uuid(pile_id, "pile_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if PileRepository(connection).get_by_id(normalized) is None:
                raise PileNotFoundError(f"pile {normalized} does not exist")
            items = RunRepository(connection).list_for_pile_limited(normalized, limit=bounded)
            steps = RunStepRepository(connection)
            return tuple(self._run_projection(item, steps.list_for_run(item.run_id)) for item in items)

    def get_run(self, run_id: str) -> RunProjection:
        normalized = self._uuid(run_id, "run_id")
        with self._connection_pool.connection() as connection:
            item = RunRepository(connection).get_by_id(normalized)
            if item is None:
                raise RunNotFoundExternalError(f"run {normalized} does not exist")
            step_items = RunStepRepository(connection).list_for_run(normalized)
        return self._run_projection(item, step_items)

    def list_run_facts(
        self,
        run_id: str,
        *,
        limit: int = 100,
    ) -> tuple[FactProjection, ...]:
        normalized = self._uuid(run_id, "run_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if RunRepository(connection).get_by_id(normalized) is None:
                raise RunNotFoundExternalError(f"run {normalized} does not exist")
            items = RunFactRepository(connection).list_reconciliation_input_for_run(normalized)[:bounded]
        return tuple(
            FactProjection(
                run_id=item.run_id,
                fact_id=item.fact_id,
                relation=item.relation,
                origin_run_id=item.origin_run_id,
                subject=item.subject,
                predicate=item.predicate,
                object_value=item.object_value,
                status=item.status,
                evidence=tuple(EvidenceProjection(**evidence.__dict__) for evidence in item.evidence),
            )
            for item in items
        )

    def list_run_findings(
        self,
        run_id: str,
        *,
        limit: int = 100,
    ) -> tuple[FindingProjection, ...]:
        normalized = self._uuid(run_id, "run_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if RunRepository(connection).get_by_id(normalized) is None:
                raise RunNotFoundExternalError(f"run {normalized} does not exist")
            items = RunFindingRepository(connection).list_review_state_for_run(normalized)[:bounded]
            links = FindingEvidenceRepository(connection)
            evidence_repository = EvidenceSpanRepository(connection)
            result: list[FindingProjection] = []
            for item in items:
                evidence: list[EvidenceProjection] = []
                for link in links.list_for_finding(item.finding_id):
                    span = evidence_repository.get_by_id(link.evidence_span_id)
                    if span is None:
                        raise ExternalQueryApplicationError(
                            f"finding {item.finding_id} references missing evidence {link.evidence_span_id}",
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
                result.append(
                    FindingProjection(
                        run_id=item.run_id,
                        finding_id=item.finding_id,
                        relation=item.relation,
                        origin_run_id=item.origin_run_id,
                        finding_type=item.finding_type,
                        rule_id=item.rule_id,
                        title=item.title,
                        description=item.description,
                        severity=item.severity,
                        status=item.status,
                        evidence=tuple(evidence),
                    ),
                )
        return tuple(result)

    def list_run_metrics(
        self,
        run_id: str,
        *,
        limit: int = 100,
    ) -> tuple[RunMetricProjection, ...]:
        normalized = self._uuid(run_id, "run_id")
        bounded = self._limit(limit)
        with self._connection_pool.connection() as connection:
            if RunRepository(connection).get_by_id(normalized) is None:
                raise RunNotFoundExternalError(f"run {normalized} does not exist")
            items = RunMetricRepository(connection).list_for_run(normalized)[:bounded]
        return tuple(RunMetricProjection(**item.__dict__) for item in items)

    @staticmethod
    def _rule_set_projection(repository: RuleSetRepository, item) -> RuleSetProjection:
        rules = tuple(RuleProjection(**rule.__dict__) for rule in repository.list_rules_for_rule_set(item.rule_set_id))
        return RuleSetProjection(**item.__dict__, rules=rules)

    @staticmethod
    def _run_projection(item, steps) -> RunProjection:
        projected_steps = tuple(
            RunStepProjection(
                step_id=step.step_id,
                stage=step.stage,
                status=step.status,
                attempt=step.attempt,
                started_at=step.started_at,
                completed_at=step.completed_at,
                error_code=step.error_code,
                error_message=step.error_message,
                retry_operation_scope=step.retry_operation_scope,
                retry_operation_key=step.retry_operation_key,
                retry_from_claim_generation=step.retry_from_claim_generation,
                created_at=step.created_at,
            )
            for step in steps
        )
        return RunProjection(**item.__dict__, steps=projected_steps)

    @staticmethod
    def _uuid(value: str, field: str) -> str:
        try:
            return str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidExternalQueryError(f"{field} must be a valid UUID") from error

    @staticmethod
    def _limit(value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 200:
            raise InvalidExternalQueryError("limit must be between 1 and 200")
        return value
