"""Transport-neutral UPDATE review facade with stale-preview protection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from api.application.build_update_change_set_service import (
    BuildUpdateChangeSetCommand,
    BuildUpdateChangeSetResult,
    BuildUpdateChangeSetService,
)
from api.application.review_update_change_set_service import (
    ReviewUpdateChangeSetCommand,
    ReviewUpdateChangeSetResult,
    ReviewUpdateChangeSetService,
)
from domain.update_review import UpdateReviewDecisionType, update_change_set_fingerprint

_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ExternalUpdateReviewError(Exception):
    pass


class InvalidExternalUpdateReviewCommandError(ExternalUpdateReviewError):
    pass


class ExternalUpdateReviewStaleError(ExternalUpdateReviewError):
    pass


@dataclass(frozen=True)
class ExternalUpdateReviewCommand:
    run_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    expected_change_set_fingerprint: str


@dataclass(frozen=True)
class ExternalUpdateReviewResult:
    change_set: BuildUpdateChangeSetResult
    review: ReviewUpdateChangeSetResult


class ExternalUpdateReviewService:
    def __init__(
        self,
        builder: BuildUpdateChangeSetService,
        reviewer: ReviewUpdateChangeSetService,
    ) -> None:
        self._builder = builder
        self._reviewer = reviewer

    def record(self, command: ExternalUpdateReviewCommand) -> ExternalUpdateReviewResult:
        if _FINGERPRINT_PATTERN.fullmatch(command.expected_change_set_fingerprint) is None:
            raise InvalidExternalUpdateReviewCommandError(
                "expected_change_set_fingerprint must be a canonical SHA-256 fingerprint",
            )
        built = self._builder.build(BuildUpdateChangeSetCommand(command.run_id))
        actual_fingerprint = update_change_set_fingerprint(built.manifest)
        if actual_fingerprint != command.expected_change_set_fingerprint:
            raise ExternalUpdateReviewStaleError(
                "UPDATE change set changed after the caller reviewed it",
            )
        reviewed = self._reviewer.record(
            ReviewUpdateChangeSetCommand(
                run_id=command.run_id,
                decision=command.decision,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
                idempotency_key=command.idempotency_key,
            ),
        )
        return ExternalUpdateReviewResult(change_set=built, review=reviewed)
