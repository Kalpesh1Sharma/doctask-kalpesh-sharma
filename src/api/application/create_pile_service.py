"""Application use case for durable Pile creation."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from repositories.pile_repository import PileRecord, PileRepository


class CreatePileApplicationError(Exception):
    """Base class for external Pile-creation failures."""


class InvalidCreatePileCommandError(CreatePileApplicationError):
    pass


@dataclass(frozen=True)
class CreatePileCommand:
    name: str
    description: str | None = None
    watch_enabled: bool = False
    watch_location: str | None = None


class CreatePileService:
    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def create(self, command: CreatePileCommand) -> PileRecord:
        normalized = self._normalize(command)
        with self._connection_pool.connection() as connection:
            record = PileRepository(connection).create(
                normalized.name,
                normalized.description,
                watch_enabled=normalized.watch_enabled,
                watch_location=normalized.watch_location,
            )
            connection.commit()
            return record

    @staticmethod
    def _normalize(command: CreatePileCommand) -> CreatePileCommand:
        if not isinstance(command.name, str) or not command.name.strip() or "\x00" in command.name:
            raise InvalidCreatePileCommandError("name must be non-empty text without NUL characters")
        name = command.name.strip()
        description = command.description
        if description is not None:
            if not isinstance(description, str) or "\x00" in description:
                raise InvalidCreatePileCommandError("description must be text without NUL characters")
            description = description.strip() or None
        if not isinstance(command.watch_enabled, bool):
            raise InvalidCreatePileCommandError("watch_enabled must be a boolean")
        watch_location = command.watch_location
        if watch_location is not None:
            if not isinstance(watch_location, str) or not watch_location.strip() or "\x00" in watch_location:
                raise InvalidCreatePileCommandError(
                    "watch_location must be non-empty text without NUL characters",
                )
            watch_location = watch_location.strip()
        if command.watch_enabled and watch_location is None:
            raise InvalidCreatePileCommandError(
                "watch_location is required when watch_enabled is true",
            )
        return CreatePileCommand(name, description, command.watch_enabled, watch_location)
