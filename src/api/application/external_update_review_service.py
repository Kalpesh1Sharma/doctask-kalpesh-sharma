"""Transport-neutral UPDATE review facade with stale-preview protection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from api.application.build_update_change_set_service import (
    BuildUpdateChangeSetCommand,
    BuildUpdateChangeSetResult,
    BuildUpdateChangeSetService,
)
from api.application.review_update_change_item_service import (
    ReviewUpdateChangeItemCommand,
    ReviewUpdateChangeItemResult,
    ReviewUpdateChangeItemService,
)
from api.application.review_update_change_set_service import (
    ReviewUpdateChangeSetCommand,
    ReviewUpdateChangeSetResult,
    ReviewUpdateChangeSetService,
)
from domain.update_review import (
    UpdateFactDeltaKind,
    UpdateReviewDecisionType,
    update_change_set_fingerprint,
)

_FINGERPRINT_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ExternalUpdateReviewError(Exception):
    pass


class InvalidExternalUpdateReviewCommandError(ExternalUpdateReviewError):
    pass


class ExternalUpdateReviewStaleError(ExternalUpdateReviewError):
    pass


class ExternalUpdateReviewItemRequiredError(ExternalUpdateReviewError):
    pass


@dataclass(frozen=True)
class ExternalUpdateReviewCommand:
    run_id: str
    decision: UpdateReviewDecisionType
    actor_type: str
    actor_id: str
    idempotency_key: str
    expected_change_set_fingerprint: str
    fact_id: str | None = None


@dataclass(frozen=True)
class ExternalUpdateReviewResult:
    change_set: BuildUpdateChangeSetResult
    review: ReviewUpdateChangeSetResult | None
    item_review: ReviewUpdateChangeItemResult | None


class ExternalUpdateReviewService:
    def __init__(
        self,
        builder: BuildUpdateChangeSetService,
        reviewer: ReviewUpdateChangeSetService,
        item_reviewer: ReviewUpdateChangeItemService | None = None,
    ) -> None:
        self._builder = builder
        self._reviewer = reviewer
        self._item_reviewer = item_reviewer

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

        material_fact_ids = {
            delta.fact_id
            for delta in getattr(built.manifest, "fact_delta", ())
            if delta.kind
            in {UpdateFactDeltaKind.CREATED, UpdateFactDeltaKind.REMOVED_FROM_CURRENT_STATE}
        }
        if command.fact_id is None:
            if material_fact_ids:
                raise ExternalUpdateReviewItemRequiredError(
                    "material UPDATE changes require one decision per Fact item",
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
            return ExternalUpdateReviewResult(
                change_set=built,
                review=reviewed,
                item_review=None,
            )

        if self._item_reviewer is None:
            raise ExternalUpdateReviewItemRequiredError(
                "item-level UPDATE review service is not configured",
            )
        item_reviewed = self._item_reviewer.record(
            ReviewUpdateChangeItemCommand(
                run_id=command.run_id,
                fact_id=command.fact_id,
                decision=command.decision,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
                idempotency_key=command.idempotency_key,
            ),
        )
        return ExternalUpdateReviewResult(
            change_set=built,
            review=None,
            item_review=item_reviewed,
        )
