"""Durable explicit acknowledgement for a zero-finding human review gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from domain.run import RunStatus, RunType
from repositories.audit_event_repository import AuditEventRecord, AuditEventRepository
from repositories.run_finding_repository import RunFindingRepository
from repositories.run_repository import RunRepository

_MAX_TRANSACTION_ATTEMPTS = 3
_KEY_PATTERN = re.compile(
    r"run-review:(?P<run_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
    r"(?P<token>[A-Za-z0-9._~-]{1,128})",
)
AUDIT_ENTITY_TYPE = "run"
AUDIT_EVENT_TYPE = "RUN_REVIEW_ACKNOWLEDGED"


class RunReviewAcknowledgementError(Exception):
    """Base class for zero-finding run-level review acknowledgement failures."""


class InvalidRunReviewAcknowledgementKeyError(RunReviewAcknowledgementError):
    pass


class RunReviewAcknowledgementRunNotFoundError(RunReviewAcknowledgementError):
    pass


class RunReviewAcknowledgementNotEligibleError(RunReviewAcknowledgementError):
    pass


class RunReviewAcknowledgementConflictError(RunReviewAcknowledgementError):
    pass


class RunReviewAcknowledgementIntegrityError(RunReviewAcknowledgementError):
    pass


@dataclass(frozen=True)
class RunReviewAcknowledgementCommand:
    run_id: str
    actor_type: str
    actor_id: str
    idempotency_key: str


@dataclass(frozen=True)
class RunReviewAcknowledgementResult:
    audit_event: AuditEventRecord
    replayed: bool


def find_run_review_acknowledgement(
    connection: Connection,
    run_id: str,
) -> AuditEventRecord | None:
    matches = [
        event
        for event in AuditEventRepository(connection).list_for_run(run_id)
        if event.entity_type == AUDIT_ENTITY_TYPE
        and event.entity_id == run_id
        and event.event_type == AUDIT_EVENT_TYPE
    ]
    if len(matches) > 1:
        raise RunReviewAcknowledgementIntegrityError(
            "run has more than one durable review acknowledgement",
        )
    return matches[0] if matches else None


class AcknowledgeRunReviewService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def record(self, command: RunReviewAcknowledgementCommand) -> RunReviewAcknowledgementResult:
        normalized = self._normalize(command)
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
        command: RunReviewAcknowledgementCommand,
    ) -> RunReviewAcknowledgementResult:
        run = RunRepository(connection).get_for_update(command.run_id)
        if run is None:
            raise RunReviewAcknowledgementRunNotFoundError(
                f"run {command.run_id} does not exist",
            )

        existing = find_run_review_acknowledgement(connection, command.run_id)
        if existing is not None:
            payload = existing.payload if isinstance(existing.payload, dict) else {}
            if (
                payload.get("idempotency_key") == command.idempotency_key
                and existing.actor_type == command.actor_type
                and existing.actor_id == command.actor_id
            ):
                return RunReviewAcknowledgementResult(existing, replayed=True)
            raise RunReviewAcknowledgementConflictError(
                "run review has already been acknowledged by a different request",
            )

        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise RunReviewAcknowledgementNotEligibleError(
                f"run {command.run_id} is not awaiting human review",
            )
        if run.run_type is RunType.UPDATE:
            raise RunReviewAcknowledgementNotEligibleError(
                "UPDATE review must use its finding/item/change-set decisions",
            )
        if RunFindingRepository(connection).list_for_run(command.run_id):
            raise RunReviewAcknowledgementNotEligibleError(
                "run-level acknowledgement is only valid when the run has zero findings",
            )

        event = AuditEventRepository(connection).create(
            command.run_id,
            AUDIT_ENTITY_TYPE,
            command.run_id,
            AUDIT_EVENT_TYPE,
            command.actor_type,
            command.actor_id,
            {
                "run_id": command.run_id,
                "decision": "ACKNOWLEDGED",
                "idempotency_key": command.idempotency_key,
            },
        )
        return RunReviewAcknowledgementResult(event, replayed=False)

    @staticmethod
    def _normalize(command: RunReviewAcknowledgementCommand) -> RunReviewAcknowledgementCommand:
        try:
            run_id = str(UUID(command.run_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidRunReviewAcknowledgementKeyError(
                "run_id must be a valid UUID",
            ) from error
        match = _KEY_PATTERN.fullmatch(command.idempotency_key)
        if match is None or match.group("run_id") != run_id:
            raise InvalidRunReviewAcknowledgementKeyError(
                "idempotency_key must be run-review:<canonical-run-uuid>:<token>",
            )
        return RunReviewAcknowledgementCommand(
            run_id=run_id,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            idempotency_key=command.idempotency_key,
        )
