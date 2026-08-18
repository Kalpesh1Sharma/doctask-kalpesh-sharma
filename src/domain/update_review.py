"""Pure deterministic contracts for immutable UPDATE review state."""

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, field_validator

from domain.fact_extraction import normalize_uuid
from domain.run_membership import RunMembershipRelation
from domain.update_preparation import UpdateDocumentChange

_FINGERPRINT_PREFIX = "sha256:"
_FINGERPRINT_LENGTH = len(_FINGERPRINT_PREFIX) + 64


class UpdateChangeSetIntegrityError(ValueError):
    """A durable update change-set cannot prove its own stored identity."""


class UpdateChangeSetInputIntegrityError(ValueError):
    """Run membership data contradicts the durable UPDATE change-set contract."""


class UpdateReviewDecisionType(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class UpdateChangeItemKind(StrEnum):
    FACT_CREATED = "FACT_CREATED"
    FACT_REMOVED = "FACT_REMOVED"


class UpdateFactDeltaKind(StrEnum):
    CARRIED_FORWARD = "CARRIED_FORWARD"
    CREATED = "CREATED"
    REMOVED_FROM_CURRENT_STATE = "REMOVED_FROM_CURRENT_STATE"


class UpdateFactDelta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    kind: UpdateFactDeltaKind

    @field_validator("fact_id")
    @classmethod
    def normalize_fact_id(cls, value: str) -> str:
        return normalize_uuid(value, "fact_id")


class UpdateChangeSetManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["update-change-set-v1"] = "update-change-set-v1"
    run_id: str
    baseline_run_id: str
    trigger_document_version_id: str
    preparation_fingerprint: str
    document_delta: tuple[UpdateDocumentChange, ...]
    fact_delta: tuple[UpdateFactDelta, ...]
    review_state_fingerprint: str
    render_input_fingerprint: str

    @field_validator("run_id", "baseline_run_id", "trigger_document_version_id")
    @classmethod
    def normalize_identifiers(cls, value: str, info) -> str:
        return normalize_uuid(value, info.field_name)

    @field_validator(
        "preparation_fingerprint",
        "review_state_fingerprint",
        "render_input_fingerprint",
    )
    @classmethod
    def validate_fingerprint(cls, value: str, info) -> str:
        if (
            len(value) != _FINGERPRINT_LENGTH
            or not value.startswith(_FINGERPRINT_PREFIX)
            or any(character not in "0123456789abcdef" for character in value[len(_FINGERPRINT_PREFIX) :])
        ):
            raise ValueError(f"{info.field_name} must be a canonical SHA-256 fingerprint")
        return value

    @field_validator("document_delta")
    @classmethod
    def sort_document_delta(
        cls,
        values: tuple[UpdateDocumentChange, ...],
    ) -> tuple[UpdateDocumentChange, ...]:
        document_ids = [value.document_id for value in values]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("document_delta cannot contain duplicate document IDs")
        return tuple(sorted(values, key=lambda value: value.document_id))

    @field_validator("fact_delta")
    @classmethod
    def sort_fact_delta(
        cls,
        values: tuple[UpdateFactDelta, ...],
    ) -> tuple[UpdateFactDelta, ...]:
        fact_ids = [value.fact_id for value in values]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("fact_delta cannot contain duplicate Fact IDs")
        return tuple(sorted(values, key=lambda value: value.fact_id))


def canonical_update_change_set_bytes(manifest: UpdateChangeSetManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def update_change_set_fingerprint(manifest: UpdateChangeSetManifest) -> str:
    return f"sha256:{hashlib.sha256(canonical_update_change_set_bytes(manifest)).hexdigest()}"


def validate_stored_update_change_set(
    manifest_value: object,
    stored_fingerprint: str,
) -> UpdateChangeSetManifest:
    """Parse and verify an immutable row before application code trusts it."""
    try:
        manifest = UpdateChangeSetManifest.model_validate(cast(JsonValue, manifest_value))
        fingerprint = update_change_set_fingerprint(manifest)
    except (TypeError, ValidationError, ValueError) as error:
        raise UpdateChangeSetIntegrityError("stored update change-set manifest is invalid") from error
    if fingerprint != stored_fingerprint:
        raise UpdateChangeSetIntegrityError("stored update change-set fingerprint does not match manifest")
    return manifest


def classify_update_fact_delta(
    baseline_fact_ids: Iterable[str],
    current_memberships: Iterable[tuple[str, RunMembershipRelation]],
) -> tuple[UpdateFactDelta, ...]:
    """Classify durable memberships without inferring relations from set overlap."""
    normalized_baseline = tuple(normalize_uuid(value, "baseline_fact_ids") for value in baseline_fact_ids)
    if len(normalized_baseline) != len(set(normalized_baseline)):
        raise UpdateChangeSetInputIntegrityError("baseline run has duplicate Fact memberships")
    baseline_ids = set(normalized_baseline)

    current: dict[str, RunMembershipRelation] = {}
    for fact_id, relation in current_memberships:
        normalized_fact_id = normalize_uuid(fact_id, "current_fact_id")
        if normalized_fact_id in current:
            raise UpdateChangeSetInputIntegrityError("UPDATE run has duplicate Fact memberships")
        current[normalized_fact_id] = relation

    delta: list[UpdateFactDelta] = []
    for fact_id, relation in current.items():
        if relation is RunMembershipRelation.CARRIED_FORWARD:
            if fact_id not in baseline_ids:
                raise UpdateChangeSetInputIntegrityError(
                    "CARRIED_FORWARD Fact membership is absent from the baseline Run",
                )
            kind = UpdateFactDeltaKind.CARRIED_FORWARD
        elif relation is RunMembershipRelation.CREATED:
            if fact_id in baseline_ids:
                raise UpdateChangeSetInputIntegrityError(
                    "CREATED Fact membership already belongs to the baseline Run",
                )
            kind = UpdateFactDeltaKind.CREATED
        else:
            raise UpdateChangeSetInputIntegrityError("unknown UPDATE Fact membership relation")
        delta.append(UpdateFactDelta(fact_id=fact_id, kind=kind))

    for fact_id in baseline_ids - current.keys():
        delta.append(
            UpdateFactDelta(
                fact_id=fact_id,
                kind=UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE,
            ),
        )
    return tuple(sorted(delta, key=lambda value: value.fact_id))
