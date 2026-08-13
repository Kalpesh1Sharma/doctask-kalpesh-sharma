from uuid import UUID

import pytest
from pydantic import ValidationError

from domain.finding import FindingStatus, FindingType
from domain.review_state import (
    ReviewStateMembership,
    ReviewStateSnapshot,
    build_review_state_snapshot,
    canonical_review_state_bytes,
    review_state_fingerprint,
)
from domain.run_membership import RunMembershipRelation

RUN_ID = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
ORIGIN_RUN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
FIRST_FINDING_ID = "11111111-1111-1111-1111-111111111111"
SECOND_FINDING_ID = "22222222-2222-2222-2222-222222222222"
RULE_ID = "33333333-3333-3333-3333-333333333333"


def membership(
    *,
    finding_id: str = FIRST_FINDING_ID,
    relation: RunMembershipRelation = RunMembershipRelation.CREATED,
    status: FindingStatus = FindingStatus.APPROVED,
    title: str = "Required clause is missing",
    description: str | None = None,
    rule_id: str | None = None,
) -> ReviewStateMembership:
    return ReviewStateMembership(
        finding_id=finding_id,
        relation=relation,
        origin_run_id=ORIGIN_RUN_ID,
        finding_type=FindingType.CONTRADICTION,
        rule_id=rule_id,
        title=title,
        description=description,
        severity="HIGH",
        status=status,
    )


def test_canonical_bytes_and_fingerprint_use_fixed_review_state_v1_format() -> None:
    snapshot = build_review_state_snapshot(RUN_ID, [membership()])

    assert snapshot.format == "review-state-v1"
    assert canonical_review_state_bytes(snapshot) == (
        b'{"format":"review-state-v1","memberships":[{"description":null,'
        b'"finding_id":"11111111-1111-1111-1111-111111111111",'
        b'"finding_type":"CONTRADICTION",'
        b'"origin_run_id":"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",'
        b'"relation":"CREATED","rule_id":null,"severity":"HIGH",'
        b'"status":"APPROVED","title":"Required clause is missing"}],'
        b'"run_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}'
    )
    assert review_state_fingerprint(snapshot) == (
        "sha256:7f2ed60e6d9ee46893e183228e40bbae60293333d5c21cd2621449f49fcff328"
    )


def test_format_is_fixed_and_rejects_arbitrary_versions() -> None:
    with pytest.raises(ValidationError):
        ReviewStateSnapshot(
            format="review-state-v2",
            run_id=RUN_ID,
            memberships=(),
        )


def test_snapshot_normalizes_uuids_sorts_memberships_and_preserves_unicode_and_nulls() -> None:
    second = membership(
        finding_id=SECOND_FINDING_ID.upper(),
        relation=RunMembershipRelation.CARRIED_FORWARD,
        description="Résumé ✓",
        rule_id=RULE_ID.upper(),
    )
    first = membership(finding_id=FIRST_FINDING_ID.upper())

    snapshot = build_review_state_snapshot(RUN_ID, [second, first])
    reordered_snapshot = build_review_state_snapshot(RUN_ID, [first, second])
    encoded = canonical_review_state_bytes(snapshot)

    assert snapshot.run_id == str(UUID(RUN_ID))
    assert [item.finding_id for item in snapshot.memberships] == [FIRST_FINDING_ID, SECOND_FINDING_ID]
    assert snapshot.memberships[1].rule_id == RULE_ID
    assert '"description":"Résumé ✓"'.encode() in encoded
    assert b'"description":null' in encoded
    assert canonical_review_state_bytes(snapshot) == canonical_review_state_bytes(reordered_snapshot)
    assert review_state_fingerprint(snapshot) == review_state_fingerprint(reordered_snapshot)


def test_duplicate_finding_memberships_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unique finding_id"):
        build_review_state_snapshot(RUN_ID, [membership(), membership()])


def test_semantic_changes_change_the_fingerprint() -> None:
    original = build_review_state_snapshot(RUN_ID, [membership()])
    changes = [
        build_review_state_snapshot(ORIGIN_RUN_ID, [membership()]),
        build_review_state_snapshot(
            RUN_ID,
            [membership(relation=RunMembershipRelation.CARRIED_FORWARD)],
        ),
        build_review_state_snapshot(RUN_ID, [membership(status=FindingStatus.REJECTED)]),
        build_review_state_snapshot(RUN_ID, [membership(title="Different immutable content")]),
    ]

    assert all(review_state_fingerprint(change) != review_state_fingerprint(original) for change in changes)


def test_is_complete_is_derived_and_not_serialized() -> None:
    pending = build_review_state_snapshot(
        RUN_ID,
        [membership(status=FindingStatus.PENDING)],
    )
    resolved = build_review_state_snapshot(RUN_ID, [membership()])
    empty = build_review_state_snapshot(RUN_ID, [])

    assert pending.is_complete is False
    assert resolved.is_complete is True
    assert empty.is_complete is True
    assert b"is_complete" not in canonical_review_state_bytes(pending)
