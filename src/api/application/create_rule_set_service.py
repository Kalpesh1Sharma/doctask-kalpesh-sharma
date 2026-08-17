"""Application use case for atomic RuleSet + Rule creation."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import JsonValue, TypeAdapter, ValidationError

from repositories.pile_repository import PileRepository
from repositories.rule_set_repository import RuleRecord, RuleSetRecord, RuleSetRepository

_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class CreateRuleSetApplicationError(Exception):
    """Base class for external RuleSet creation failures."""


class InvalidCreateRuleSetCommandError(CreateRuleSetApplicationError):
    pass


class CreateRuleSetPileNotFoundError(CreateRuleSetApplicationError):
    pass


@dataclass(frozen=True)
class CreateRuleCommand:
    rule_key: str
    title: str
    description: str | None
    severity: str
    definition: JsonValue


@dataclass(frozen=True)
class CreateRuleSetCommand:
    pile_id: str
    name: str
    description: str | None
    version: int
    rules: tuple[CreateRuleCommand, ...]


@dataclass(frozen=True)
class CreateRuleSetResult:
    rule_set: RuleSetRecord
    rules: tuple[RuleRecord, ...]


class CreateRuleSetService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def create(self, command: CreateRuleSetCommand) -> CreateRuleSetResult:
        normalized = self._normalize(command)
        with self._connection_pool.connection() as connection:
            if PileRepository(connection).get_for_update(normalized.pile_id) is None:
                raise CreateRuleSetPileNotFoundError(
                    f"pile {normalized.pile_id} does not exist",
                )
            repository = RuleSetRepository(connection)
            rule_set = repository.create_rule_set(
                normalized.pile_id,
                normalized.name,
                normalized.description,
                normalized.version,
            )
            rules = tuple(
                repository.create_rule(
                    rule_set.rule_set_id,
                    rule_key=rule.rule_key,
                    title=rule.title,
                    description=rule.description,
                    severity=rule.severity,
                    definition=rule.definition,
                )
                for rule in normalized.rules
            )
            connection.commit()
            return CreateRuleSetResult(rule_set, rules)

    @classmethod
    def _normalize(cls, command: CreateRuleSetCommand) -> CreateRuleSetCommand:
        try:
            pile_id = str(UUID(command.pile_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise InvalidCreateRuleSetCommandError("pile_id must be a valid UUID") from error
        name = cls._required_text(command.name, "name")
        description = cls._optional_text(command.description, "description")
        if not isinstance(command.version, int) or isinstance(command.version, bool) or command.version <= 0:
            raise InvalidCreateRuleSetCommandError("version must be a positive integer")
        if not command.rules:
            raise InvalidCreateRuleSetCommandError("rules must contain at least one rule")
        normalized_rules: list[CreateRuleCommand] = []
        seen_keys: set[str] = set()
        for item in command.rules:
            rule_key = cls._required_text(item.rule_key, "rule_key")
            if rule_key in seen_keys:
                raise InvalidCreateRuleSetCommandError("rule_key values must be unique within a rule set")
            seen_keys.add(rule_key)
            try:
                definition = _JSON_VALUE_ADAPTER.validate_python(item.definition)
            except ValidationError as error:
                raise InvalidCreateRuleSetCommandError(
                    "definition must be a JSON-compatible value",
                ) from error
            normalized_rules.append(
                CreateRuleCommand(
                    rule_key=rule_key,
                    title=cls._required_text(item.title, "title"),
                    description=cls._optional_text(item.description, "description"),
                    severity=cls._required_text(item.severity, "severity"),
                    definition=definition,
                ),
            )
        return CreateRuleSetCommand(
            pile_id,
            name,
            description,
            command.version,
            tuple(normalized_rules),
        )

    @staticmethod
    def _required_text(value: object, field: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise InvalidCreateRuleSetCommandError(
                f"{field} must be non-empty text without NUL characters",
            )
        return value.strip()

    @staticmethod
    def _optional_text(value: object, field: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or "\x00" in value:
            raise InvalidCreateRuleSetCommandError(f"{field} must be text without NUL characters")
        return value.strip() or None
