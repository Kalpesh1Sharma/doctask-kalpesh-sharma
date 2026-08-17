from __future__ import annotations

from dataclasses import dataclass

from api.application.document_arrival_service import DocumentArrivalResult
from api.application.ingest_and_reconcile_document_service import (
    IngestAndReconcileDocumentCommand,
    IngestAndReconcileDocumentService,
)
from api.application.ingest_document_service import IngestDocumentResult


@dataclass
class _Ingestion:
    replayed: bool
    calls: int = 0

    def ingest(self, command) -> IngestDocumentResult:
        self.calls += 1
        assert command.logical_key == "contract"
        return IngestDocumentResult(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "sha256:" + "a" * 64,
            "source://sha256/" + "a" * 64,
            replayed=self.replayed,
        )


@dataclass
class _Arrival:
    calls: int = 0

    def reconcile_pile(self, pile_id: str) -> DocumentArrivalResult:
        self.calls += 1
        return DocumentArrivalResult(
            pile_id,
            ("33333333-3333-3333-3333-333333333333",),
            None,
        )


def test_facade_reconciles_after_fresh_ingestion() -> None:
    ingestion = _Ingestion(False)
    arrival = _Arrival()
    service = IngestAndReconcileDocumentService(ingestion, arrival)  # type: ignore[arg-type]

    result = service.ingest(
        IngestAndReconcileDocumentCommand(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "contract",
            b"hello",
        ),
    )

    assert result.replayed is False
    assert result.created_run_ids == ("33333333-3333-3333-3333-333333333333",)
    assert ingestion.calls == 1
    assert arrival.calls == 1


def test_facade_also_reconciles_exact_replay_to_repair_prior_ready_arrival_gap() -> None:
    ingestion = _Ingestion(True)
    arrival = _Arrival()
    service = IngestAndReconcileDocumentService(ingestion, arrival)  # type: ignore[arg-type]

    result = service.ingest(
        IngestAndReconcileDocumentCommand(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "contract",
            b"hello",
        ),
    )

    assert result.replayed is True
    assert arrival.calls == 1
