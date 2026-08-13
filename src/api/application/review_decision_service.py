import re
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from domain.finding import FindingStatus, validate_status_transition
from domain.review_decision import ReviewDecisionType
from domain.run import RunStatus
from repositories.audit_event_repository import AuditEventRecord, AuditEventRepository
from repositories.finding_repository import FindingRecord, FindingRepository
from repositories.review_decision_repository import (
    ReviewDecisionRecord,
    ReviewDecisionRepository,
)
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRepository

_IDEMPOTENCY_KEY_PATTERN = re.compile(
    r"review:(?P<run_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
    r"(?P<token>[A-Za-z0-9._~-]{1,128})"
)
_MAX_TRANSACTION_ATTEMPTS = 3
_DECISION_AUDIT_ENTITY_TYPE = "finding"
_DECISION_AUDIT_EVENT_TYPE = "REVIEW_DECISION_RECORDED"


class ReviewDecisionApplicationError(Exception):
    """Base class for item-level review application errors."""


class InvalidReviewIdempotencyKeyError(ReviewDecisionApplicationError):
    pass


class RunNotFoundError(ReviewDecisionApplicationError):
    pass


class RunNotReviewableError(ReviewDecisionApplicationError):
    pass


class FindingNotFoundError(ReviewDecisionApplicationError):
    pass


class FindingNotInRunError(ReviewDecisionApplicationError):
    pass


class TerminalFindingError(ReviewDecisionApplicationError):
    pass


class ReviewIdempotencyConflictError(ReviewDecisionApplicationError):
    pass


@dataclass(frozen=True)
class ReviewDecisionCommand:
    run_id: str
    finding_id: str
    decision: ReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str


@dataclass(frozen=True)
class ReviewDecisionResult:
    review_decision: ReviewDecisionRecord
    finding: FindingRecord
    decision_audit_event: AuditEventRecord | None
    review_complete: bool
    replayed: bool


class ReviewDecisionService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def record(self, command: ReviewDecisionCommand) -> ReviewDecisionResult:
        normalized_command = self._normalize_command(command)

        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    result = self._record_once(connection, normalized_command)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise

        raise AssertionError("unreachable")

    def _record_once(
        self,
        connection: Connection,
        command: ReviewDecisionCommand,
    ) -> ReviewDecisionResult:
        run_repository = RunRepository(connection)
        finding_repository = FindingRepository(connection)
        run_finding_repository = RunFindingRepository(connection)
        review_decision_repository = ReviewDecisionRepository(connection)
        audit_event_repository = AuditEventRepository(connection)

        run = run_repository.get_for_update(command.run_id)
        if run is None:
            raise RunNotFoundError(f"run {command.run_id} does not exist")

        existing_decision = review_decision_repository.get_by_idempotency_key(
            command.idempotency_key,
        )
        finding = finding_repository.get_by_id(command.finding_id)
        membership = run_finding_repository.get(command.run_id, command.finding_id)

        if existing_decision is not None:
            if not self._matches_replay(existing_decision, command):
                raise ReviewIdempotencyConflictError(
                    "idempotency key is already used by a different review decision",
                )
            if finding is None:
                raise FindingNotFoundError(f"finding {command.finding_id} does not exist")
            if membership is None:
                raise FindingNotInRunError(
                    f"finding {command.finding_id} is not a member of run {command.run_id}",
                )

            return ReviewDecisionResult(
                review_decision=existing_decision,
                finding=finding,
                decision_audit_event=None,
                review_complete=not run_finding_repository.has_pending_for_run(command.run_id),
                replayed=True,
            )

        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise RunNotReviewableError(
                f"run {command.run_id} is not awaiting human review",
            )
        if finding is None:
            raise FindingNotFoundError(f"finding {command.finding_id} does not exist")
        if membership is None:
            raise FindingNotInRunError(
                f"finding {command.finding_id} is not a member of run {command.run_id}",
            )

        requested_status = FindingStatus(command.decision.value)
        if (
            finding.status is not FindingStatus.PENDING
            or not validate_status_transition(finding.status, requested_status)
        ):
            raise TerminalFindingError(
                f"finding {command.finding_id} is already resolved",
            )

        updated_finding = finding_repository.update_status(
            command.finding_id,
            requested_status,
        )
        assert updated_finding is not None
        review_decision = review_decision_repository.create(
            command.finding_id,
            command.decision,
            command.actor_type,
            command.actor_id,
            command.idempotency_key,
        )
        decision_audit_event = audit_event_repository.create(
            command.run_id,
            _DECISION_AUDIT_ENTITY_TYPE,
            command.finding_id,
            _DECISION_AUDIT_EVENT_TYPE,
            command.actor_type,
            command.actor_id,
            {
                "run_id": command.run_id,
                "finding_id": command.finding_id,
                "decision": command.decision.value,
                "review_decision_id": review_decision.review_decision_id,
                "idempotency_key": command.idempotency_key,
            },
        )

        return ReviewDecisionResult(
            review_decision=review_decision,
            finding=updated_finding,
            decision_audit_event=decision_audit_event,
            review_complete=not run_finding_repository.has_pending_for_run(command.run_id),
            replayed=False,
        )

    def _normalize_command(self, command: ReviewDecisionCommand) -> ReviewDecisionCommand:
        try:
            normalized_run_id = str(UUID(command.run_id))
        except ValueError as error:
            raise InvalidReviewIdempotencyKeyError("run_id must be a valid UUID") from error

        match = _IDEMPOTENCY_KEY_PATTERN.fullmatch(command.idempotency_key)
        if match is None or match.group("run_id") != normalized_run_id:
            raise InvalidReviewIdempotencyKeyError(
                "idempotency_key must be review:<canonical-run-uuid>:<token>",
            )

        return ReviewDecisionCommand(
            run_id=normalized_run_id,
            finding_id=command.finding_id,
            decision=command.decision,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            idempotency_key=command.idempotency_key,
        )

    def _matches_replay(
        self,
        existing_decision: ReviewDecisionRecord,
        command: ReviewDecisionCommand,
    ) -> bool:
        return (
            existing_decision.finding_id == command.finding_id
            and existing_decision.decision is command.decision
            and existing_decision.actor_type == command.actor_type
            and existing_decision.actor_id == command.actor_id
        )
