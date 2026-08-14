import pytest
from pydantic import ValidationError

from domain.update_preparation import (
    UpdateDocumentChange,
    UpdateDocumentChangeKind,
    UpdatePreparationManifest,
    canonical_update_preparation_bytes,
    update_preparation_fingerprint,
)

RUN = "00000000-0000-0000-0000-000000000001"
BASELINE = "00000000-0000-0000-0000-000000000002"
TRIGGER = "00000000-0000-0000-0000-000000000003"
DOCUMENT_A = "00000000-0000-0000-0000-000000000004"
DOCUMENT_B = "00000000-0000-0000-0000-000000000005"
VERSION_A = "00000000-0000-0000-0000-000000000006"
VERSION_B_OLD = "00000000-0000-0000-0000-000000000007"
FACT = "00000000-0000-0000-0000-000000000008"


def manifest() -> UpdatePreparationManifest:
    return UpdatePreparationManifest(
        run_id=RUN.upper(),
        baseline_run_id=BASELINE,
        trigger_document_version_id=TRIGGER,
        document_changes=(
            UpdateDocumentChange(
                document_id=DOCUMENT_B,
                kind=UpdateDocumentChangeKind.REPLACED,
                baseline_document_version_id=VERSION_B_OLD,
                target_document_version_id=TRIGGER,
                target_version_number=2,
                target_content_hash="sha256:b",
            ),
            UpdateDocumentChange(
                document_id=DOCUMENT_A,
                kind=UpdateDocumentChangeKind.RETAINED,
                baseline_document_version_id=VERSION_A,
                target_document_version_id=VERSION_A,
                target_version_number=1,
                target_content_hash="sha256:a",
            ),
        ),
        carried_fact_ids=(FACT.upper(),),
    )


def test_manifest_normalizes_and_orders_deterministically() -> None:
    value = manifest()
    assert value.run_id == RUN
    assert [change.document_id for change in value.document_changes] == [DOCUMENT_A, DOCUMENT_B]
    assert value.carried_fact_ids == (FACT,)
    assert canonical_update_preparation_bytes(value) == canonical_update_preparation_bytes(manifest())
    assert update_preparation_fingerprint(value) == update_preparation_fingerprint(manifest())


def test_fingerprint_changes_for_semantic_preparation_input() -> None:
    value = manifest()
    changed = value.model_copy(update={"trigger_document_version_id": VERSION_A})
    assert update_preparation_fingerprint(value) != update_preparation_fingerprint(changed)


def test_added_change_is_valid_and_canonical() -> None:
    value = UpdatePreparationManifest(
        run_id=RUN,
        baseline_run_id=BASELINE,
        trigger_document_version_id=TRIGGER,
        document_changes=(UpdateDocumentChange(
            document_id=DOCUMENT_A,
            kind=UpdateDocumentChangeKind.ADDED,
            baseline_document_version_id=None,
            target_document_version_id=TRIGGER,
            target_version_number=1,
            target_content_hash="sha256:added",
        ),),
        carried_fact_ids=(),
    )
    assert value.document_changes[0].kind is UpdateDocumentChangeKind.ADDED


def test_manifest_rejects_duplicate_documents_and_invalid_change_shapes() -> None:
    value = manifest()
    with pytest.raises(ValidationError):
        UpdatePreparationManifest.model_validate({
            **value.model_dump(mode="json"),
            "document_changes": value.model_dump(mode="json")["document_changes"]
            + [value.model_dump(mode="json")["document_changes"][0]],
        })
    with pytest.raises(ValidationError):
        UpdateDocumentChange(
            document_id=DOCUMENT_A,
            kind=UpdateDocumentChangeKind.ADDED,
            baseline_document_version_id=VERSION_A,
            target_document_version_id=TRIGGER,
            target_version_number=1,
            target_content_hash="sha256:x",
        )
    with pytest.raises(ValidationError):
        UpdatePreparationManifest.model_validate({
            **value.model_dump(mode="json"),
            "carried_fact_ids": [FACT, FACT.upper()],
        })
    with pytest.raises(ValidationError):
        UpdateDocumentChange(
            document_id=DOCUMENT_A,
            kind=UpdateDocumentChangeKind.RETAINED,
            baseline_document_version_id=VERSION_A,
            target_document_version_id=TRIGGER,
            target_version_number=1,
            target_content_hash="sha256:x",
        )
