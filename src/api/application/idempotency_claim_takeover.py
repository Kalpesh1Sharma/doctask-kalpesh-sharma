"""Shared policy and persistence arbitration for fenced stale claims."""

from datetime import timedelta

from repositories.idempotency_repository import (
    IdempotencyRecord,
    IdempotencyRepository,
    InProgressClaimState,
)

DEFAULT_STALE_CLAIM_AFTER = timedelta(minutes=5)


def validate_stale_claim_after(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError("stale_claim_after must be a positive timedelta")
    return value


def reclaim_if_stale(
    repository: IdempotencyRepository,
    record: IdempotencyRecord,
    stale_claim_after: timedelta,
) -> IdempotencyRecord | None:
    """Attempt the repository's fenced stale reclaim for an observed owner."""
    return repository.reclaim_stale(
        record.scope,
        record.key,
        record.claim_generation,
        stale_claim_after,
    )


def classify_in_progress(
    repository: IdempotencyRepository,
    record: IdempotencyRecord,
    stale_claim_after: timedelta,
) -> InProgressClaimState:
    return repository.classify_in_progress(
        record.scope,
        record.key,
        record.claim_generation,
        stale_claim_after,
    )
