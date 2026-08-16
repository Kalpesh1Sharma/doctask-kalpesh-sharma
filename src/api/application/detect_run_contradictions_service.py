"""Application use case for grounded contradiction detection during EXAMINE."""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.idempotency_claim_takeover import (
    DEFAULT_STALE_CLAIM_AFTER,
    classify_in_progress,
    reclaim_if_stale,
    validate_stale_claim_after,
)
from domain.contradiction_detection import (
    ContradictionCandidatePair,
    ContradictionDetectionRequest,
    ContradictionDetectionResponse,
    ContradictionSourceEvidence,
    ContradictionSourceFact,
    canonical_json_value,
    contradiction_detection_input_fingerprint,
    normalize_uuid,
)
from domain.fact import FactStatus
from domain.finding import Finding, FindingStatus, FindingType
from domain.run import RunStatus
from domain.run_membership import RunMembershipRelation
from repositories.finding_evidence_repository import FindingEvidenceRepository
from repositories.finding_repository import FindingRepository
from repositories.idempotency_repository import (
    IdempotencyFailureKind,
    IdempotencyRecord,
    IdempotencyRepository,
    IdempotencyStatus,
    InProgressClaimState,
)
from repositories.run_fact_repository import RunFactReconciliationRecord, RunFactRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRecord, RunRepository
from services.contradiction_detection_provider import (
    ContradictionDetectionProvider,
    ContradictionDetectionProviderError,
)

_IDEMPOTENCY_SCOPE = "contradiction-detection-v1"
_MAX_TRANSACTION_ATTEMPTS = 3
_RESULT_REFERENCE_PATTERN = re.compile(
    r"^contradiction-detection:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}:sha256:[0-9a-f]{64}$",
)


class ContradictionDetectionApplicationError(Exception):
    """Base class for contradiction-detection failures."""


class InvalidContradictionDetectionCommandError(ContradictionDetectionApplicationError):
    pass


class ContradictionDetectionRunNotFoundError(ContradictionDetectionApplicationError):
    pass


class ContradictionDetectionRunNotEligibleError(ContradictionDetectionApplicationError):
    pass


class ContradictionInputIntegrityError(ContradictionDetectionApplicationError):
    pass


class ContradictionInputConflictError(ContradictionDetectionApplicationError):
    pass


class ContradictionIdempotencyIntegrityError(ContradictionDetectionApplicationError):
    pass


class ContradictionDetectionInProgressError(ContradictionDetectionApplicationError):
    pass


class ContradictionDetectionPreviouslyFailedError(ContradictionDetectionApplicationError):
    pass


class InvalidContradictionDetectionResponseError(ContradictionDetectionApplicationError):
    pass


class ContradictionInputDriftError(ContradictionDetectionApplicationError):
    pass


class ContradictionClaimLostError(ContradictionDetectionApplicationError):
    pass


class ContradictionDetectionProviderFailure(ContradictionDetectionApplicationError):
    pass


@dataclass(frozen=True)
class DetectRunContradictionsCommand:
    run_id: str


@dataclass(frozen=True)
class DetectRunContradictionsResult:
    run_id: str
    input_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _ClaimedOperation:
    request: ContradictionDetectionRequest
    input_fingerprint: str
    claim_token: int


@dataclass(frozen=True)
class _FindingPlan:
    finding: Finding
    evidence_ids: tuple[str, ...]


class DetectRunContradictionsService:
    def __init__(
        self,
        connection_pool: ConnectionPool,
        provider: ContradictionDetectionProvider,
        trusted_instructions: str,
        instruction_version: str,
        *,
        stale_claim_after: timedelta = DEFAULT_STALE_CLAIM_AFTER,
    ) -> None:
        self._connection_pool = connection_pool
        self._provider = provider
        self._trusted_instructions = trusted_instructions
        self._instruction_version = instruction_version
        self._stale_claim_after = validate_stale_claim_after(stale_claim_after)

    def detect(
        self,
        command: DetectRunContradictionsCommand,
    ) -> DetectRunContradictionsResult:
        normalized_command = DetectRunContradictionsCommand(
            run_id=self._normalize_run_id(command.run_id),
        )
        claimed = self._claim_or_replay(normalized_command)
        if isinstance(claimed, DetectRunContradictionsResult):
            return claimed

        return self._execute_claimed(claimed)

    def _execute_claimed(self, claimed: _ClaimedOperation) -> DetectRunContradictionsResult:
        try:
            response = self._provider.detect(claimed.request)
            finding_plans = self._build_finding_plans(claimed.request, response)
        except InvalidContradictionDetectionResponseError:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_INVALID_RESPONSE)
            raise
        except ContradictionDetectionApplicationError:
            self._mark_failed(claimed)
            raise
        except ContradictionDetectionProviderError as error:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE)
            raise ContradictionDetectionProviderFailure(
                "contradiction detection provider failed",
            ) from error
        except Exception as error:
            self._mark_failed(claimed, IdempotencyFailureKind.RETRYABLE_PROVIDER_FAILURE)
            raise ContradictionDetectionProviderFailure(
                "contradiction detection provider failed",
            ) from error

        self._persist(claimed, finding_plans)
        return DetectRunContradictionsResult(
            run_id=claimed.request.run_id,
            input_fingerprint=claimed.input_fingerprint,
            replayed=False,
        )

    def _retry_after_failed_from_workflow(
        self,
        command: DetectRunContradictionsCommand,
        *,
        expected_failed_generation: int,
    ) -> DetectRunContradictionsResult:
        run_id = self._normalize_run_id(command.run_id)
        with self._connection_pool.connection() as connection:
            run = RunRepository(connection).get_for_update(run_id)
            if run is None:
                raise ContradictionDetectionRunNotFoundError(f"run {run_id} does not exist")
            repository = IdempotencyRepository(connection)
            failed = repository.get(_IDEMPOTENCY_SCOPE, self._idempotency_key(run_id))
            if (
                failed is None or failed.status is not IdempotencyStatus.FAILED
                or failed.claim_generation != expected_failed_generation or failed.failure_kind is None
            ):
                return self._resolve_authoritative_existing(connection, run, repository)
            if run.status is not RunStatus.RUNNING:
                raise ContradictionDetectionRunNotEligibleError("run is not running contradiction detection")
            request = self._load_request(connection, run_id)
            fingerprint = contradiction_detection_input_fingerprint(request)
            reopened = repository.reopen_failed(
                _IDEMPOTENCY_SCOPE, self._idempotency_key(run_id), expected_failed_generation, failed.failure_kind,
            )
            if reopened is None:
                return self._resolve_authoritative_existing(connection, run, repository)
            connection.commit()
        return self._execute_claimed(_ClaimedOperation(request, fingerprint, reopened.claim_generation))

    def _claim_or_replay(
        self,
        command: DetectRunContradictionsCommand,
    ) -> _ClaimedOperation | DetectRunContradictionsResult:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    outcome = self._claim_or_replay_once(connection, command)
                    connection.commit()
                    return outcome
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _claim_or_replay_once(
        self,
        connection: Connection,
        command: DetectRunContradictionsCommand,
    ) -> _ClaimedOperation | DetectRunContradictionsResult:
        run = RunRepository(connection).get_for_update(command.run_id)
        if run is None:
            raise ContradictionDetectionRunNotFoundError(f"run {command.run_id} does not exist")

        idempotency = IdempotencyRepository(connection)
        key = self._idempotency_key(command.run_id)
        existing = idempotency.get(_IDEMPOTENCY_SCOPE, key)
        if existing is None:
            if run.status is not RunStatus.RUNNING:
                raise ContradictionDetectionRunNotEligibleError(f"run {command.run_id} is not running contradiction detection")
            request = self._load_request(connection, command.run_id)
            fingerprint = contradiction_detection_input_fingerprint(request)
            claimed = idempotency.claim(_IDEMPOTENCY_SCOPE, key)
            if claimed is not None:
                return _ClaimedOperation(request, fingerprint, claimed.claim_generation)
            return self._resolve_authoritative_existing(connection, run, idempotency)
        if existing.status is IdempotencyStatus.FAILED:
            raise ContradictionDetectionPreviouslyFailedError("contradiction detection previously failed")
        if existing.status is IdempotencyStatus.COMPLETED:
            return self._resolve_completed_existing(connection, run, existing)
        state = classify_in_progress(idempotency, existing, self._stale_claim_after)
        if state is InProgressClaimState.MATCHING_FRESH:
            raise ContradictionDetectionInProgressError("contradiction detection is already in progress")
        if state is InProgressClaimState.CHANGED_OR_MISSING:
            return self._resolve_authoritative_existing(connection, run, idempotency)
        if run.status is not RunStatus.RUNNING:
            raise ContradictionDetectionRunNotEligibleError(f"run {command.run_id} is not running contradiction detection")
        request = self._load_request(connection, command.run_id)
        fingerprint = contradiction_detection_input_fingerprint(request)
        claimed = reclaim_if_stale(idempotency, existing, self._stale_claim_after)
        if claimed is not None:
            return _ClaimedOperation(request, fingerprint, claimed.claim_generation)
        return self._resolve_authoritative_existing(connection, run, idempotency)

    def _resolve_authoritative_existing(
        self, connection: Connection, run: RunRecord, idempotency: IdempotencyRepository,
    ) -> DetectRunContradictionsResult:
        current = idempotency.get(_IDEMPOTENCY_SCOPE, self._idempotency_key(run.run_id))
        if current is None:
            raise ContradictionIdempotencyIntegrityError("claimed operation disappeared")
        if current.status is IdempotencyStatus.FAILED:
            raise ContradictionDetectionPreviouslyFailedError("contradiction detection previously failed")
        if current.status is IdempotencyStatus.IN_PROGRESS:
            raise ContradictionDetectionInProgressError("contradiction detection is already in progress")
        return self._resolve_completed_existing(connection, run, current)

    def _resolve_completed_existing(
        self, connection: Connection, run: RunRecord, existing: IdempotencyRecord,
    ) -> DetectRunContradictionsResult:
        request = self._load_request(connection, run.run_id)
        fingerprint = contradiction_detection_input_fingerprint(request)
        return self._resolve_existing(existing, request, fingerprint, self._result_reference(run.run_id, fingerprint))

    def _resolve_existing(
        self,
        existing: IdempotencyRecord,
        request: ContradictionDetectionRequest,
        fingerprint: str,
        result_reference: str,
    ) -> DetectRunContradictionsResult:
        if existing.status is IdempotencyStatus.COMPLETED:
            if existing.result_reference is None or not _RESULT_REFERENCE_PATTERN.fullmatch(
                existing.result_reference,
            ):
                raise ContradictionIdempotencyIntegrityError(
                    "completed contradiction detection has a malformed result reference",
                )
            if existing.result_reference != result_reference:
                raise ContradictionInputConflictError(
                    "the run already completed contradiction detection for different Fact input",
                )
            return DetectRunContradictionsResult(request.run_id, fingerprint, replayed=True)
        if existing.status is IdempotencyStatus.IN_PROGRESS:
            raise ContradictionDetectionInProgressError("contradiction detection is already in progress")
        raise ContradictionDetectionPreviouslyFailedError("contradiction detection previously failed")

    def _persist(
        self,
        claimed: _ClaimedOperation,
        finding_plans: tuple[_FindingPlan, ...],
    ) -> None:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    self._persist_once(connection, claimed, finding_plans)
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _persist_once(
        self,
        connection: Connection,
        claimed: _ClaimedOperation,
        finding_plans: tuple[_FindingPlan, ...],
    ) -> None:
        run = RunRepository(connection).get_for_update(claimed.request.run_id)
        if run is None:
            raise ContradictionDetectionRunNotFoundError(
                f"run {claimed.request.run_id} does not exist",
            )
        if run.status is not RunStatus.RUNNING:
            raise ContradictionDetectionRunNotEligibleError(
                f"run {claimed.request.run_id} is no longer running contradiction detection",
            )

        current_request = self._load_request(connection, claimed.request.run_id)
        if contradiction_detection_input_fingerprint(current_request) != claimed.input_fingerprint:
            raise ContradictionInputDriftError(
                "run Fact input changed while provider work was in progress",
            )

        finding_repository = FindingRepository(connection)
        run_finding_repository = RunFindingRepository(connection)
        finding_evidence_repository = FindingEvidenceRepository(connection)
        for finding_plan in finding_plans:
            stored = finding_repository.create(finding_plan.finding)
            run_finding_repository.add(
                claimed.request.run_id,
                stored.finding_id,
                RunMembershipRelation.CREATED,
            )
            for evidence_id in finding_plan.evidence_ids:
                finding_evidence_repository.add(stored.finding_id, evidence_id)

        completed = IdempotencyRepository(connection).mark_completed(
            _IDEMPOTENCY_SCOPE,
            self._idempotency_key(claimed.request.run_id),
            claimed.claim_token,
            self._result_reference(claimed.request.run_id, claimed.input_fingerprint),
        )
        if completed is None:
            raise ContradictionClaimLostError("contradiction detection claim is no longer owned")

    def _mark_failed(
        self,
        claimed: _ClaimedOperation,
        failure_kind: IdempotencyFailureKind | None = None,
    ) -> None:
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    failed = IdempotencyRepository(connection).mark_failed(
                        _IDEMPOTENCY_SCOPE,
                        self._idempotency_key(claimed.request.run_id),
                        claimed.claim_token,
                        failure_kind,
                    )
                    if failed is None:
                        raise ContradictionClaimLostError("contradiction detection claim is no longer owned")
                    connection.commit()
                    return
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _load_request(self, connection: Connection, run_id: str) -> ContradictionDetectionRequest:
        records = RunFactRepository(connection).list_reconciliation_input_for_run(run_id)
        source_facts = tuple(
            self._to_source_fact(record)
            for record in records
            if record.status in {FactStatus.CONFIRMED, FactStatus.DISPUTED}
        )
        for fact in source_facts:
            if not fact.evidence:
                raise ContradictionInputIntegrityError(
                    f"Fact {fact.fact_id} has no durable supporting evidence",
                )
        return ContradictionDetectionRequest(
            run_id=run_id,
            instruction_version=self._instruction_version,
            trusted_instructions=self._trusted_instructions,
            source_facts=source_facts,
            candidate_pairs=self._candidate_pairs(source_facts),
        )

    def _to_source_fact(
        self,
        record: RunFactReconciliationRecord,
    ) -> ContradictionSourceFact:
        return ContradictionSourceFact(
            fact_id=record.fact_id,
            origin_run_id=record.origin_run_id,
            relation=record.relation,
            subject=record.subject,
            predicate=record.predicate,
            object_value=record.object_value,
            status=record.status,
            evidence=tuple(
                ContradictionSourceEvidence(
                    evidence_id=evidence.evidence_id,
                    document_version_id=evidence.document_version_id,
                    chunk_id=evidence.chunk_id,
                    page_number=evidence.page_number,
                    start_offset=evidence.start_offset,
                    end_offset=evidence.end_offset,
                    source_text=evidence.source_text,
                    content_hash=evidence.content_hash,
                )
                for evidence in record.evidence
            ),
        )

    def _candidate_pairs(
        self,
        facts: Iterable[ContradictionSourceFact],
    ) -> tuple[ContradictionCandidatePair, ...]:
        grouped: dict[tuple[str, str], list[ContradictionSourceFact]] = {}
        for fact in facts:
            grouped.setdefault((fact.subject, fact.predicate), []).append(fact)

        pairs: list[ContradictionCandidatePair] = []
        for group in grouped.values():
            for index, left in enumerate(group):
                for right in group[index + 1 :]:
                    if canonical_json_value(left.object_value) == canonical_json_value(
                        right.object_value,
                    ):
                        continue
                    pairs.append(
                        ContradictionCandidatePair(
                            fact_a_id=left.fact_id,
                            fact_b_id=right.fact_id,
                        ),
                    )
        return tuple(sorted(pairs, key=lambda pair: pair.identity))

    def _build_finding_plans(
        self,
        request: ContradictionDetectionRequest,
        response: ContradictionDetectionResponse,
    ) -> tuple[_FindingPlan, ...]:
        if not isinstance(response, ContradictionDetectionResponse):
            raise InvalidContradictionDetectionResponseError(
                "provider returned an invalid response type",
            )

        facts_by_id = {fact.fact_id: fact for fact in request.source_facts}
        allowed_pairs = {pair.identity for pair in request.candidate_pairs}
        plans: list[_FindingPlan] = []
        for pair in response.contradictions:
            if pair.fact_a_id not in facts_by_id or pair.fact_b_id not in facts_by_id:
                raise InvalidContradictionDetectionResponseError(
                    "provider referenced a Fact outside the request",
                )
            if pair.identity not in allowed_pairs:
                raise InvalidContradictionDetectionResponseError(
                    "provider referenced a Fact pair outside the candidate set",
                )
            left = facts_by_id[pair.fact_a_id]
            right = facts_by_id[pair.fact_b_id]
            evidence_ids = tuple(sorted({
                *(evidence.evidence_id for evidence in left.evidence),
                *(evidence.evidence_id for evidence in right.evidence),
            }))
            if not evidence_ids:
                raise ContradictionInputIntegrityError(
                    "contradiction candidates require evidence on both Facts",
                )
            description = (
                f"Conflicting Fact IDs: {left.fact_id}, {right.fact_id}; values: "
                f"{canonical_json_value(left.object_value)} vs "
                f"{canonical_json_value(right.object_value)}."
            )
            plans.append(
                _FindingPlan(
                    finding=Finding(
                        finding_id=str(uuid4()),
                        origin_run_id=request.run_id,
                        finding_type=FindingType.CONTRADICTION,
                        rule_id=None,
                        title=f"Contradiction: {left.subject} / {left.predicate}",
                        description=description,
                        severity="MEDIUM",
                        status=FindingStatus.PENDING,
                    ),
                    evidence_ids=evidence_ids,
                ),
            )
        return tuple(plans)

    def _normalize_run_id(self, value: str) -> str:
        try:
            return normalize_uuid(value, "run_id")
        except ValueError as error:
            raise InvalidContradictionDetectionCommandError(
                "run_id must be a valid UUID",
            ) from error

    def _idempotency_key(self, run_id: str) -> str:
        return f"examine-contradictions:{run_id}"

    def _result_reference(self, run_id: str, fingerprint: str) -> str:
        return f"contradiction-detection:{run_id}:{fingerprint}"
