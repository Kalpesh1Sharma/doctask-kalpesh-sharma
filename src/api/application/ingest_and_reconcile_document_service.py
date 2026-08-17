"""External ingestion facade: durable ingestion followed by lineage reconciliation."""

from __future__ import annotations

from dataclasses import dataclass

from api.application.document_arrival_service import DocumentArrivalService
from api.application.ingest_document_service import IngestDocumentCommand, IngestDocumentService
from services.local_source_storage import SourceInput


@dataclass(frozen=True)
class IngestAndReconcileDocumentCommand:
    pile_id: str
    logical_key: str
    source: SourceInput


@dataclass(frozen=True)
class IngestAndReconcileDocumentResult:
    document_id: str
    document_version_id: str
    content_hash: str
    storage_uri: str
    replayed: bool
    created_run_ids: tuple[str, ...]
    reconciled_blocked_run_id: str | None


class IngestAndReconcileDocumentService:
    """Compose existing ingestion and arrival use cases without duplicating either."""

    def __init__(
        self,
        ingestion_service: IngestDocumentService,
        arrival_service: DocumentArrivalService,
    ) -> None:
        self._ingestion = ingestion_service
        self._arrival = arrival_service

    def ingest(
        self,
        command: IngestAndReconcileDocumentCommand,
    ) -> IngestAndReconcileDocumentResult:
        ingestion = self._ingestion.ingest(
            IngestDocumentCommand(command.pile_id, command.logical_key, command.source),
        )
        # Always reconcile after a successful exact replay too.  This repairs a
        # prior process crash between READY persistence and lineage creation.
        arrival = self._arrival.reconcile_pile(command.pile_id)
        return IngestAndReconcileDocumentResult(
            document_id=ingestion.document_id,
            document_version_id=ingestion.document_version_id,
            content_hash=ingestion.content_hash,
            storage_uri=ingestion.storage_uri,
            replayed=ingestion.replayed,
            created_run_ids=arrival.created_run_ids,
            reconciled_blocked_run_id=arrival.reconciled_blocked_run_id,
        )
