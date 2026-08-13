from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from domain.finding import FindingStatus, FindingType
from domain.run_membership import RunMembershipRelation

_REVIEW_STATE_FORMAT = "review-state-v1"


def _canonical_uuid(value: str) -> str:
    return str(UUID(value))


class ReviewStateMembership(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_id: str
    relation: RunMembershipRelation
    origin_run_id: str
    finding_type: FindingType
    rule_id: str | None
    title: str
    description: str | None
    severity: str
    status: FindingStatus

    @field_validator("finding_id", "origin_run_id", "rule_id")
    @classmethod
    def normalize_uuid(cls, value: str | None) -> str | None:
        return _canonical_uuid(value) if value is not None else None


class ReviewStateSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    format: Literal["review-state-v1"] = _REVIEW_STATE_FORMAT
    run_id: str
    memberships: tuple[ReviewStateMembership, ...]

    @field_validator("run_id")
    @classmethod
    def normalize_run_id(cls, value: str) -> str:
        return _canonical_uuid(value)

    @field_validator("memberships")
    @classmethod
    def sort_and_validate_memberships(
        cls,
        memberships: tuple[ReviewStateMembership, ...],
    ) -> tuple[ReviewStateMembership, ...]:
        sorted_memberships = tuple(sorted(memberships, key=lambda membership: membership.finding_id))
        finding_ids = [membership.finding_id for membership in sorted_memberships]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("review state memberships must have unique finding_id values")

        return sorted_memberships

    @property
    def is_complete(self) -> bool:
        return all(membership.status is not FindingStatus.PENDING for membership in self.memberships)


def build_review_state_snapshot(
    run_id: str,
    memberships: tuple[ReviewStateMembership, ...] | list[ReviewStateMembership],
) -> ReviewStateSnapshot:
    return ReviewStateSnapshot(run_id=run_id, memberships=tuple(memberships))


def canonical_review_state_bytes(snapshot: ReviewStateSnapshot) -> bytes:
    value = {
        "format": snapshot.format,
        "run_id": snapshot.run_id,
        "memberships": [
            {
                "finding_id": membership.finding_id,
                "relation": membership.relation.value,
                "origin_run_id": membership.origin_run_id,
                "finding_type": membership.finding_type.value,
                "rule_id": membership.rule_id,
                "title": membership.title,
                "description": membership.description,
                "severity": membership.severity,
                "status": membership.status.value,
            }
            for membership in snapshot.memberships
        ],
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def review_state_fingerprint(snapshot: ReviewStateSnapshot) -> str:
    return f"sha256:{sha256(canonical_review_state_bytes(snapshot)).hexdigest()}"
