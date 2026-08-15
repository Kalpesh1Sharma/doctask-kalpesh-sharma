import re
from dataclasses import dataclass

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from api.application.build_update_change_set_service import (
    UpdateChangeSetRunNotEligibleError,
    build_current_update_change_set_manifest,
    normalize_run_id,
)
from domain.run import RunStatus
from domain.update_review import UpdateReviewDecisionType, update_change_set_fingerprint
from repositories.audit_event_repository import AuditEventRecord, AuditEventRepository
from repositories.run_repository import RunRepository
from repositories.update_change_set_repository import (
    UpdateChangeSetRecord,
    UpdateChangeSetRepository,
)
from repositories.update_review_decision_repository import (
    UpdateReviewDecisionRecord,
    UpdateReviewDecisionRepository,
)

_MAX_TRANSACTION_ATTEMPTS = 3
_KEY_PATTERN = re.compile(
    r"update-review:(?P<run_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
    r"(?P<token>[A-Za-z0-9._~-]{1,128})",
)
_AUDIT_ENTITY_TYPE = "update_change_set"
_AUDIT_EVENT_TYPE = "UPDATE_REVIEW_DECISION_RECORDED"


class UpdateReviewApplicationError(Exception):
    """Base class for UPDATE-level human review failures."""


class UpdateReviewRunNotFoundError(UpdateReviewApplicationError):
    pass


class UpdateReviewChangeSetMissingError(UpdateReviewApplicationError):
    pass


class UpdateReviewIdempotencyKeyError(UpdateReviewApplicationError):
    pass


class UpdateReviewIdempotencyConflictError(UpdateReviewApplicationError):
    pass


class UpdateChangeSetAlreadyReviewedError(UpdateReviewApplicationError):
    pass


class UpdateReviewRunNotEligibleError(UpdateReviewApplicationError):
    pass


class UpdateChangeSetStaleError(UpdateReviewApplicationError):
    pass


@dataclass(frozen=True)
class ReviewUpdateChangeSetCommand:
    run_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str


@dataclass(frozen=True)
class ReviewUpdateChangeSetResult:
    change_set: UpdateChangeSetRecord
    decision: UpdateReviewDecisionRecord
    audit_event: AuditEventRecord | None
    replayed: bool


class ReviewUpdateChangeSetService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def record(self, command: ReviewUpdateChangeSetCommand) -> ReviewUpdateChangeSetResult:
        normalized = normalize_command(command)
        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    result = self._record_once(connection, normalized)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def _record_once(
        self,
        connection: Connection,
        command: ReviewUpdateChangeSetCommand,
    ) -> ReviewUpdateChangeSetResult:
        run = RunRepository(connection).get_for_update(command.run_id)
        if run is None:
            raise UpdateReviewRunNotFoundError(f"run {command.run_id} does not exist")

        change_set_repository = UpdateChangeSetRepository(connection)
        decision_repository = UpdateReviewDecisionRepository(connection)
        change_set = change_set_repository.get_by_run_id(command.run_id)
        if change_set is None:
            raise UpdateReviewChangeSetMissingError("UPDATE Run has no durable change set")
        change_set.to_manifest()

        existing_by_key = decision_repository.get_by_idempotency_key(command.idempotency_key)
        if existing_by_key is not None:
            if not matches_replay(existing_by_key, change_set, command):
                raise UpdateReviewIdempotencyConflictError(
                    "idempotency key is already used by a different UPDATE review request",
                )
            return ReviewUpdateChangeSetResult(change_set, existing_by_key, None, replayed=True)

        terminal_decision = decision_repository.get_by_change_set_id(change_set.change_set_id)
        if terminal_decision is not None:
            raise UpdateChangeSetAlreadyReviewedError("UPDATE change set already has a terminal decision")

        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise UpdateReviewRunNotEligibleError("UPDATE Run is not awaiting review")
        try:
            current_manifest = build_current_update_change_set_manifest(connection, run)
        except UpdateChangeSetRunNotEligibleError as error:
            raise UpdateReviewRunNotEligibleError(str(error)) from error
        if update_change_set_fingerprint(current_manifest) != change_set.change_set_fingerprint:
            raise UpdateChangeSetStaleError("UPDATE change set no longer matches current state")

        decision = decision_repository.create(
            change_set.change_set_id,
            command.decision,
            command.actor_type,
            command.actor_id,
            command.idempotency_key,
        )
        audit_event = AuditEventRepository(connection).create(
            command.run_id,
            _AUDIT_ENTITY_TYPE,
            change_set.change_set_id,
            _AUDIT_EVENT_TYPE,
            command.actor_type,
            command.actor_id,
            audit_payload(command, change_set),
        )
        return ReviewUpdateChangeSetResult(change_set, decision, audit_event, replayed=False)


def normalize_command(command: ReviewUpdateChangeSetCommand) -> ReviewUpdateChangeSetCommand:
    run_id = normalize_run_id(command.run_id)
    match = _KEY_PATTERN.fullmatch(command.idempotency_key)
    if match is None or match.group("run_id") != run_id:
        raise UpdateReviewIdempotencyKeyError(
            "idempotency_key must be update-review:<canonical-run-uuid>:<token>",
        )
    return ReviewUpdateChangeSetCommand(
        run_id=run_id,
        decision=command.decision,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        idempotency_key=command.idempotency_key,
    )


def matches_replay(
    decision: UpdateReviewDecisionRecord,
    change_set: UpdateChangeSetRecord,
    command: ReviewUpdateChangeSetCommand,
) -> bool:
    return (
        decision.change_set_id == change_set.change_set_id
        and decision.decision is command.decision
        and decision.actor_type == command.actor_type
        and decision.actor_id == command.actor_id
    )


def audit_payload(
    command: ReviewUpdateChangeSetCommand,
    change_set: UpdateChangeSetRecord,
) -> dict[str, str]:
    return {
        "run_id": command.run_id,
        "change_set_id": change_set.change_set_id,
        "change_set_fingerprint": change_set.change_set_fingerprint,
        "preparation_fingerprint": change_set.preparation_fingerprint,
        "review_state_fingerprint": change_set.review_state_fingerprint,
        "render_input_fingerprint": change_set.render_input_fingerprint,
        "decision": command.decision.value,
        "idempotency_key": command.idempotency_key,
    }
