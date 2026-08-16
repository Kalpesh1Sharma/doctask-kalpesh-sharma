"""At-least-once PostgreSQL reconciliation from READY source state to Runs."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import ConnectionPool

from domain.document_version import DocumentProcessingStatus
from domain.run import Run, RunStage, RunStatus, RunType
from repositories.document_repository import CurrentDocumentVersionRecord, DocumentRepository
from repositories.document_version_repository import DocumentVersionRepository
from repositories.pile_repository import PileRepository
from repositories.run_document_version_repository import RunDocumentVersionRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.run_step_repository import RunStepRepository

_MAX_ATTEMPTS = 3
_STAGES = (
    RunStage.INGEST,
    RunStage.UNDERSTAND,
    RunStage.EXAMINE,
    RunStage.REVIEW,
    RunStage.COMMIT,
)


class DocumentArrivalError(Exception):
    pass


class DocumentArrivalPileNotFoundError(DocumentArrivalError):
    pass


class PileTailIntegrityError(DocumentArrivalError):
    pass


@dataclass(frozen=True)
class DocumentArrivalResult:
    pile_id: str
    created_run_ids: tuple[str, ...]
    reconciled_blocked_run_id: str | None


class DocumentArrivalService:
    """Coalesce current document state while serializing on ``piles``."""

    def __init__(self, connection_pool: ConnectionPool) -> None:
        self._connection_pool = connection_pool

    def reconcile_pile(self, pile_id: str) -> DocumentArrivalResult:
        try:
            normalized = str(UUID(pile_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise DocumentArrivalPileNotFoundError("pile_id must be a valid UUID") from error
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with self._connection_pool.connection() as connection:
                    result = self._reconcile_once(connection, normalized)
                    connection.commit()
                    return result
            except (DeadlockDetected, SerializationFailure):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")

    def reconcile_ready_piles(self, *, limit: int = 100) -> tuple[DocumentArrivalResult, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self._connection_pool.connection() as connection:
            pile_ids = PileRepository(connection).list_ids_with_ready_current_versions(limit)
            connection.commit()
        return tuple(self.reconcile_pile(pile_id) for pile_id in pile_ids)

    def _reconcile_once(self, connection: Connection, pile_id: str) -> DocumentArrivalResult:
        piles = PileRepository(connection)
        pile = piles.get_for_update(pile_id)
        if pile is None:
            raise DocumentArrivalPileNotFoundError(f"pile {pile_id} does not exist")
        runs = RunRepository(connection)
        documents = DocumentRepository(connection)
        current = documents.list_current_versions_for_pile(pile_id)
        if any(item.processing_status != DocumentProcessingStatus.READY.value for item in current):
            raise PileTailIntegrityError("Document current_version_id is not READY")
        if pile.latest_run_id is None:
            if runs.list_for_pile(pile_id):
                raise PileTailIntegrityError("pile has Runs but no authoritative latest_run_id")
            if not current:
                return DocumentArrivalResult(pile_id, (), None)
            initial = self._create_initial(connection, pile_id, current)
            updated = piles.set_latest_run(pile_id, initial.run_id)
            assert updated is not None
            return DocumentArrivalResult(pile_id, (initial.run_id,), None)

        tail = runs.get_for_update(pile.latest_run_id)
        if tail is None or tail.pile_id != pile_id:
            raise PileTailIntegrityError("latest_run_id is absent or belongs to another pile")
        reconciled = self._resolve_blocked_tail(connection, tail)
        if reconciled is not None:
            tail = reconciled

        represented = self._represented_document_map(connection, tail)
        desired = {item.document_id: item for item in current}
        changes = tuple(
            item for document_id, item in sorted(desired.items())
            if represented.get(document_id) != item.document_version_id
        )
        created: list[str] = []
        for current_item in changes:
            child = self._create_update(connection, pile_id, tail, current_item)
            updated = piles.set_latest_run(pile_id, child.run_id)
            assert updated is not None
            tail = child
            represented[current_item.document_id] = current_item.document_version_id
            created.append(child.run_id)
        return DocumentArrivalResult(
            pile_id,
            tuple(created),
            reconciled.run_id if reconciled is not None else None,
        )

    def _create_initial(
        self,
        connection: Connection,
        pile_id: str,
        current: list[CurrentDocumentVersionRecord],
    ) -> RunRecord:
        run = RunRepository(connection).create(
            Run(run_id=str(uuid4()), pile_id=pile_id, run_type=RunType.INITIAL),
        )
        membership = RunDocumentVersionRepository(connection)
        for item in current:
            membership.add(run.run_id, item.document_version_id)
        self._create_five_step_graph(connection, run.run_id)
        return run

    def _create_update(
        self,
        connection: Connection,
        pile_id: str,
        tail: RunRecord,
        current: CurrentDocumentVersionRecord,
    ) -> RunRecord:
        if tail.status is RunStatus.COMPLETED:
            baseline_run_id = tail.run_id
            blocked_on_run_id = None
        elif tail.status is RunStatus.FAILED:
            raise PileTailIntegrityError("automatic lineage tail is FAILED")
        else:
            baseline_run_id = None
            blocked_on_run_id = tail.run_id
        run = RunRepository(connection).create(
            Run(
                run_id=str(uuid4()),
                pile_id=pile_id,
                run_type=RunType.UPDATE,
                trigger_document_version_id=current.document_version_id,
                parent_run_id=tail.run_id,
                baseline_run_id=baseline_run_id,
                blocked_on_run_id=blocked_on_run_id,
            ),
        )
        self._create_five_step_graph(connection, run.run_id)
        return run

    @staticmethod
    def _create_five_step_graph(connection: Connection, run_id: str) -> None:
        steps = RunStepRepository(connection)
        for stage in _STAGES:
            steps.create(run_id, stage, 1)

    def _resolve_blocked_tail(self, connection: Connection, tail: RunRecord) -> RunRecord | None:
        if tail.run_type is not RunType.UPDATE or tail.blocked_on_run_id is None:
            return None
        parent = RunRepository(connection).get_for_update(tail.blocked_on_run_id)
        if parent is None or parent.pile_id != tail.pile_id:
            raise PileTailIntegrityError("blocked automatic UPDATE has invalid parent")
        if parent.status is not RunStatus.COMPLETED:
            return None
        updated = RunRepository(connection).resolve_blocked_update_baseline(tail.run_id, parent.run_id)
        if updated is None:
            raise PileTailIntegrityError("blocked automatic UPDATE lineage changed during reconciliation")
        return updated

    def _represented_document_map(self, connection: Connection, tail: RunRecord) -> dict[str, str]:
        """Resolve intended current state through the parent chain, never history order."""
        seen: set[str] = set()

        def resolve(run: RunRecord) -> dict[str, str]:
            if run.run_id in seen:
                raise PileTailIntegrityError("automatic lineage has a parent cycle")
            seen.add(run.run_id)
            scope = RunDocumentVersionRepository(connection).list_scope_for_run(run.run_id)
            if scope:
                map_from_scope: dict[str, str] = {}
                for item in scope:
                    if (
                        item.document_pile_id != run.pile_id
                        or item.processing_status is not DocumentProcessingStatus.READY
                        or item.document_id in map_from_scope
                    ):
                        raise PileTailIntegrityError("Run scope is not one READY version per pile Document")
                    map_from_scope[item.document_id] = item.document_version_id
                return map_from_scope
            if run.run_type is RunType.INITIAL:
                raise PileTailIntegrityError("INITIAL lineage tail has no durable source scope")
            if run.parent_run_id is None:
                raise PileTailIntegrityError("automatic UPDATE lineage has no parent")
            parent = RunRepository(connection).get_by_id(run.parent_run_id)
            if parent is None or parent.pile_id != run.pile_id:
                raise PileTailIntegrityError("automatic UPDATE parent is invalid")
            base = resolve(parent)
            if run.run_type is not RunType.UPDATE or run.trigger_document_version_id is None:
                raise PileTailIntegrityError("non-scope lineage child is not an UPDATE trigger")
            trigger = DocumentVersionRepository(connection).get_by_id(run.trigger_document_version_id)
            if trigger is None:
                raise PileTailIntegrityError("automatic UPDATE trigger disappeared")
            document = DocumentRepository(connection).get_by_id(trigger.document_id)
            if document is None or document.pile_id != run.pile_id:
                raise PileTailIntegrityError("automatic UPDATE trigger belongs to another pile")
            base[trigger.document_id] = trigger.document_version_id
            return base

        return resolve(tail)
