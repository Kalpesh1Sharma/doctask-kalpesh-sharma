from __future__ import annotations

from dataclasses import dataclass

from api.application.classify_document_version_service import ClassifyDocumentVersionResult
from api.application.document_arrival_service import DocumentArrivalResult
from api.application.ingest_and_reconcile_document_service import (
    IngestAndReconcileDocumentCommand,
    IngestAndReconcileDocumentService,
)
from api.application.ingest_document_service import IngestDocumentResult
from domain.document_classification import DocumentRole


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


class _Classification:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.calls = 0

    def classify(self, command) -> ClassifyDocumentVersionResult:
        self.calls += 1
        self.order.append("classify")
        assert command.document_version_id == "22222222-2222-2222-2222-222222222222"
        return ClassifyDocumentVersionResult(
            classification_id="44444444-4444-4444-8444-444444444444",
            document_version_id=command.document_version_id,
            role=DocumentRole.PROMISSORY_NOTE,
            confidence=0.95,
            explanation="Grounded note classification.",
            evidence_chunk_ids=("55555555-5555-4555-8555-555555555555",),
            provider_name="test",
            model_name="test-model",
            input_tokens=4,
            output_tokens=2,
            replayed=False,
        )


class _OrderedArrival(_Arrival):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    def reconcile_pile(self, pile_id: str) -> DocumentArrivalResult:
        self.order.append("arrival")
        return super().reconcile_pile(pile_id)


def test_facade_classifies_ready_version_before_arrival_reconciliation() -> None:
    order: list[str] = []
    ingestion = _Ingestion(False)
    classification = _Classification(order)
    arrival = _OrderedArrival(order)
    service = IngestAndReconcileDocumentService(
        ingestion,
        arrival,  # type: ignore[arg-type]
        classification,  # type: ignore[arg-type]
    )

    result = service.ingest(
        IngestAndReconcileDocumentCommand(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "contract",
            b"hello",
        ),
    )

    assert order == ["classify", "arrival"]
    assert classification.calls == 1
    assert result.classification is not None
    assert result.classification.role is DocumentRole.PROMISSORY_NOTE
