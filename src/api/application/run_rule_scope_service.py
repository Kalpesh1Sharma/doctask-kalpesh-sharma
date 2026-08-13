from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from domain.run import RunStatus
from domain.run_rule_scope import RunRuleSetSnapshot, RunRuleSnapshot
from repositories.rule_source_repository import (
    RuleSetSourceRecord,
    RuleSourceRecord,
    RuleSourceRepository,
)
from repositories.run_repository import RunRepository
from repositories.run_rule_scope_repository import (
    RunRuleScopeRecord,
    RunRuleScopeRepository,
    RunRuleSetSnapshotRecord,
    RunRuleSnapshotRecord,
)

_MAX_TRANSACTION_ATTEMPTS = 3


class RunRuleScopeApplicationError(Exception):
    """Base class for Run rule-scope capture errors."""


class InvalidRunRuleScopeSelectionError(RunRuleScopeApplicationError):
    pass


class RunRuleScopeRunNotFoundError(RunRuleScopeApplicationError):
    pass


class RunRuleScopeRunNotEligibleError(RunRuleScopeApplicationError):
    pass


class RuleSetNotFoundError(RunRuleScopeApplicationError):
    pass


class RuleNotFoundError(RunRuleScopeApplicationError):
    pass


class RuleSetPileMismatchError(RunRuleScopeApplicationError):
    pass


class RuleSelectionMismatchError(RunRuleScopeApplicationError):
    pass


class RunRuleScopeConflictError(RunRuleScopeApplicationError):
    pass


@dataclass(frozen=True)
class CaptureRunRuleScopeCommand:
    run_id: str
    source_rule_set_ids: tuple[str, ...]
    source_rule_ids: tuple[str, ...]


@dataclass(frozen=True)
class CaptureRunRuleScopeResult:
    scope: RunRuleScopeRecord
    rule_set_snapshots: tuple[RunRuleSetSnapshotRecord, ...]
    rule_snapshots: tuple[RunRuleSnapshotRecord, ...]
    replayed: bool


class RunRuleScopeService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def capture(self, command: CaptureRunRuleScopeCommand) -> CaptureRunRuleScopeResult:
        normalized_command = self._normalize_command(command)

        for attempt in range(_MAX_TRANSACTION_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    result = self._capture_once(connection, normalized_command)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_TRANSACTION_ATTEMPTS - 1:
                    raise

        raise AssertionError("unreachable")

    def _capture_once(
        self,
        connection: Connection,
        command: CaptureRunRuleScopeCommand,
    ) -> CaptureRunRuleScopeResult:
        run_repository = RunRepository(connection)
        scope_repository = RunRuleScopeRepository(connection)

        run = run_repository.get_for_update(command.run_id)
        if run is None:
            raise RunRuleScopeRunNotFoundError(f"run {command.run_id} does not exist")

        scope = scope_repository.get_scope(command.run_id)
        if scope is not None:
            return self._replay_or_conflict(scope_repository, scope, command)

        if run.status is not RunStatus.PENDING:
            raise RunRuleScopeRunNotEligibleError(
                f"run {command.run_id} is not pending rule-scope capture",
            )

        source_repository = RuleSourceRepository(connection)
        rule_sets = source_repository.load_rule_sets_for_share(command.source_rule_set_ids)
        rules = source_repository.load_rules_for_share(command.source_rule_ids)

        self._validate_sources(command, run.pile_id, rule_sets, rules)

        stored_rule_sets = tuple(
            scope_repository.insert_rule_set_snapshot(
                RunRuleSetSnapshot(
                    run_id=command.run_id,
                    source_rule_set_id=rule_set.rule_set_id,
                    name=rule_set.name,
                    description=rule_set.description,
                    version=rule_set.version,
                ),
            )
            for rule_set in rule_sets
        )
        stored_rules = tuple(
            scope_repository.insert_rule_snapshot(
                RunRuleSnapshot(
                    run_id=command.run_id,
                    source_rule_id=rule.rule_id,
                    source_rule_set_id=rule.rule_set_id,
                    rule_key=rule.rule_key,
                    title=rule.title,
                    description=rule.description,
                    severity=rule.severity,
                    definition=rule.definition,
                ),
            )
            for rule in rules
        )
        completed_scope = scope_repository.complete_scope(command.run_id)

        return CaptureRunRuleScopeResult(
            scope=completed_scope,
            rule_set_snapshots=stored_rule_sets,
            rule_snapshots=stored_rules,
            replayed=False,
        )

    def _replay_or_conflict(
        self,
        scope_repository: RunRuleScopeRepository,
        scope: RunRuleScopeRecord,
        command: CaptureRunRuleScopeCommand,
    ) -> CaptureRunRuleScopeResult:
        rule_sets = tuple(scope_repository.list_rule_sets_for_run(command.run_id))
        rules = tuple(scope_repository.list_rules_for_run(command.run_id))
        captured_rule_set_ids = {record.source_rule_set_id for record in rule_sets}
        captured_rule_ids = {record.source_rule_id for record in rules}

        if (
            captured_rule_set_ids != set(command.source_rule_set_ids)
            or captured_rule_ids != set(command.source_rule_ids)
        ):
            raise RunRuleScopeConflictError(
                f"run {command.run_id} already has a different closed rule scope",
            )

        return CaptureRunRuleScopeResult(
            scope=scope,
            rule_set_snapshots=rule_sets,
            rule_snapshots=rules,
            replayed=True,
        )

    def _validate_sources(
        self,
        command: CaptureRunRuleScopeCommand,
        run_pile_id: str,
        rule_sets: Sequence[RuleSetSourceRecord],
        rules: Sequence[RuleSourceRecord],
    ) -> None:
        if len(rule_sets) != len(command.source_rule_set_ids):
            found_ids = {rule_set.rule_set_id for rule_set in rule_sets}
            missing_ids = sorted(set(command.source_rule_set_ids) - found_ids)
            raise RuleSetNotFoundError(f"rule sets do not exist: {', '.join(missing_ids)}")
        if len(rules) != len(command.source_rule_ids):
            found_ids = {rule.rule_id for rule in rules}
            missing_ids = sorted(set(command.source_rule_ids) - found_ids)
            raise RuleNotFoundError(f"rules do not exist: {', '.join(missing_ids)}")

        for rule_set in rule_sets:
            if rule_set.pile_id != run_pile_id:
                raise RuleSetPileMismatchError(
                    f"rule set {rule_set.rule_set_id} does not belong to run {command.run_id} pile",
                )

        selected_rule_set_ids = set(command.source_rule_set_ids)
        for rule in rules:
            if rule.rule_set_id not in selected_rule_set_ids:
                raise RuleSelectionMismatchError(
                    f"rule {rule.rule_id} belongs to an unselected rule set {rule.rule_set_id}",
                )

    def _normalize_command(
        self,
        command: CaptureRunRuleScopeCommand,
    ) -> CaptureRunRuleScopeCommand:
        return CaptureRunRuleScopeCommand(
            run_id=self._normalize_uuid(command.run_id, "run_id"),
            source_rule_set_ids=self._normalize_uuid_set(
                command.source_rule_set_ids,
                "source_rule_set_ids",
            ),
            source_rule_ids=self._normalize_uuid_set(
                command.source_rule_ids,
                "source_rule_ids",
            ),
        )

    def _normalize_uuid_set(self, values: Sequence[str], field_name: str) -> tuple[str, ...]:
        normalized_values = tuple(self._normalize_uuid(value, field_name) for value in values)
        if len(set(normalized_values)) != len(normalized_values):
            raise InvalidRunRuleScopeSelectionError(f"{field_name} cannot contain duplicate UUIDs")
        return tuple(sorted(normalized_values))

    def _normalize_uuid(self, value: str, field_name: str) -> str:
        try:
            return str(UUID(value))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidRunRuleScopeSelectionError(
                f"{field_name} must contain valid UUIDs",
            ) from error
