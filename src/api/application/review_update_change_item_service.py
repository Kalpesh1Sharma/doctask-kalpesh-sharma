import re
from dataclasses import dataclass
from uuid import UUID

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
from repositories.update_change_item_repository import (
    UpdateChangeItemRecord,
    UpdateChangeItemRepository,
)
from repositories.update_change_set_repository import (
    UpdateChangeSetRecord,
    UpdateChangeSetRepository,
)
from repositories.update_item_review_decision_repository import (
    UpdateItemReviewDecisionRecord,
    UpdateItemReviewDecisionRepository,
)

_MAX_TRANSACTION_ATTEMPTS = 3
_KEY_PATTERN = re.compile(
    r"update-item-review:(?P<run_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
    r"(?P<fact_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):"
    r"(?P<token>[A-Za-z0-9._~-]{1,128})",
)
_AUDIT_ENTITY_TYPE = "update_change_item"
_AUDIT_EVENT_TYPE = "UPDATE_ITEM_REVIEW_DECISION_RECORDED"


class UpdateItemReviewApplicationError(Exception):
    pass


class UpdateItemReviewRunNotFoundError(UpdateItemReviewApplicationError):
    pass


class UpdateItemReviewChangeSetMissingError(UpdateItemReviewApplicationError):
    pass


class UpdateItemReviewItemNotFoundError(UpdateItemReviewApplicationError):
    pass


class UpdateItemReviewIdempotencyKeyError(UpdateItemReviewApplicationError):
    pass


class UpdateItemReviewIdempotencyConflictError(UpdateItemReviewApplicationError):
    pass


class UpdateItemAlreadyReviewedError(UpdateItemReviewApplicationError):
    pass


class UpdateItemReviewRunNotEligibleError(UpdateItemReviewApplicationError):
    pass


class UpdateItemReviewChangeSetStaleError(UpdateItemReviewApplicationError):
    pass


@dataclass(frozen=True)
class ReviewUpdateChangeItemCommand:
    run_id: str
    fact_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str


@dataclass(frozen=True)
class ReviewUpdateChangeItemResult:
    change_set: UpdateChangeSetRecord
    item: UpdateChangeItemRecord
    decision: UpdateItemReviewDecisionRecord
    audit_event: AuditEventRecord | None
    review_complete: bool
    replayed: bool


class ReviewUpdateChangeItemService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def record(self, command: ReviewUpdateChangeItemCommand) -> ReviewUpdateChangeItemResult:
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
        command: ReviewUpdateChangeItemCommand,
    ) -> ReviewUpdateChangeItemResult:
        run = RunRepository(connection).get_for_update(command.run_id)
        if run is None:
            raise UpdateItemReviewRunNotFoundError(f"run {command.run_id} does not exist")

        change_set = UpdateChangeSetRepository(connection).get_by_run_id(command.run_id)
        if change_set is None:
            raise UpdateItemReviewChangeSetMissingError("UPDATE Run has no durable change set")
        change_set.to_manifest()

        item_repository = UpdateChangeItemRepository(connection)
        item = item_repository.get_by_change_set_and_fact_id(change_set.change_set_id, command.fact_id)
        if item is None:
            raise UpdateItemReviewItemNotFoundError(
                f"Fact {command.fact_id} is not an item in this UPDATE change set",
            )

        decision_repository = UpdateItemReviewDecisionRepository(connection)
        existing_by_key = decision_repository.get_by_idempotency_key(command.idempotency_key)
        if existing_by_key is not None:
            if not matches_replay(existing_by_key, item, command):
                raise UpdateItemReviewIdempotencyConflictError(
                    "idempotency key is already used by a different UPDATE item review request",
                )
            return ReviewUpdateChangeItemResult(
                change_set,
                item,
                existing_by_key,
                None,
                review_complete=self._is_complete(connection, change_set.change_set_id),
                replayed=True,
            )

        terminal = decision_repository.get_by_item_id(item.item_id)
        if terminal is not None:
            raise UpdateItemAlreadyReviewedError("UPDATE change item already has a terminal decision")

        if run.status is not RunStatus.REVIEW_REQUIRED:
            raise UpdateItemReviewRunNotEligibleError("UPDATE Run is not awaiting review")
        try:
            current_manifest = build_current_update_change_set_manifest(connection, run)
        except UpdateChangeSetRunNotEligibleError as error:
            raise UpdateItemReviewRunNotEligibleError(str(error)) from error
        if update_change_set_fingerprint(current_manifest) != change_set.change_set_fingerprint:
            raise UpdateItemReviewChangeSetStaleError(
                "UPDATE change set no longer matches current state",
            )

        decision = decision_repository.create(
            item.item_id,
            command.decision,
            command.actor_type,
            command.actor_id,
            command.idempotency_key,
        )
        audit_event = AuditEventRepository(connection).create(
            command.run_id,
            _AUDIT_ENTITY_TYPE,
            item.item_id,
            _AUDIT_EVENT_TYPE,
            command.actor_type,
            command.actor_id,
            audit_payload(command, change_set, item),
        )
        return ReviewUpdateChangeItemResult(
            change_set,
            item,
            decision,
            audit_event,
            review_complete=self._is_complete(connection, change_set.change_set_id),
            replayed=False,
        )

    @staticmethod
    def _is_complete(connection: Connection, change_set_id: str) -> bool:
        items = UpdateChangeItemRepository(connection).list_for_change_set(change_set_id)
        decisions = UpdateItemReviewDecisionRepository(connection).list_for_change_set(change_set_id)
        return len(items) == len(decisions)


def normalize_command(command: ReviewUpdateChangeItemCommand) -> ReviewUpdateChangeItemCommand:
    run_id = normalize_run_id(command.run_id)
    try:
        fact_id = str(UUID(command.fact_id))
    except (AttributeError, TypeError, ValueError) as error:
        raise UpdateItemReviewItemNotFoundError("fact_id must be a valid UUID") from error
    match = _KEY_PATTERN.fullmatch(command.idempotency_key)
    if (
        match is None
        or match.group("run_id") != run_id
        or match.group("fact_id") != fact_id
    ):
        raise UpdateItemReviewIdempotencyKeyError(
            "idempotency_key must be update-item-review:<run-id>:<fact-id>:<token>",
        )
    return ReviewUpdateChangeItemCommand(
        run_id=run_id,
        fact_id=fact_id,
        decision=command.decision,
        actor_type=command.actor_type,
        actor_id=command.actor_id,
        idempotency_key=command.idempotency_key,
    )


def matches_replay(
    decision: UpdateItemReviewDecisionRecord,
    item: UpdateChangeItemRecord,
    command: ReviewUpdateChangeItemCommand,
) -> bool:
    return (
        decision.item_id == item.item_id
        and decision.decision is command.decision
        and decision.actor_type == command.actor_type
        and decision.actor_id == command.actor_id
    )


def audit_payload(
    command: ReviewUpdateChangeItemCommand,
    change_set: UpdateChangeSetRecord,
    item: UpdateChangeItemRecord,
) -> dict[str, str]:
    return {
        "run_id": command.run_id,
        "change_set_id": change_set.change_set_id,
        "change_set_fingerprint": change_set.change_set_fingerprint,
        "item_id": item.item_id,
        "fact_id": item.fact_id,
        "item_kind": item.kind.value,
        "decision": command.decision.value,
        "idempotency_key": command.idempotency_key,
    }
