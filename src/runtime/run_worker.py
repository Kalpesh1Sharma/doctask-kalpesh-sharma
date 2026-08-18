"""Polling background Run worker layered over the durable workflow service."""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from psycopg_pool import ConnectionPool

from api.application.external_advance_run_service import (
    ExternalAdvanceRunService,
    ExternalHumanReviewRequiredError,
)
from api.application.run_workflow_service import (
    RunWorkflowAwaitingUpstream,
    RunWorkflowCommand,
    RunWorkflowOperationInProgress,
    RunWorkflowOutcome,
    RunWorkflowRunFailedError,
    RunWorkflowStepEscalatedError,
)
from domain.run import RunStage, RunStatus, RunType
from repositories.rule_set_repository import RuleSetRepository
from repositories.run_repository import RunRecord, RunRepository
from repositories.run_rule_scope_repository import RunRuleScopeRepository
from runtime.run_worker_claims import PostgresRunWorkerClaimCoordinator, RunWorkerClaim

_LOG = logging.getLogger("task1.worker")


@dataclass(frozen=True)
class WorkerRuleSelection:
    source_rule_set_ids: tuple[str, ...]
    source_rule_ids: tuple[str, ...]


class RunWorkerStatus(StrEnum):
    REACHED_REVIEW = "REACHED_REVIEW"
    WAITING_REVIEW = "WAITING_REVIEW"
    COMPLETED = "COMPLETED"
    WAITING_UPSTREAM = "WAITING_UPSTREAM"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    YIELDED = "YIELDED"
    CLAIM_UNAVAILABLE = "CLAIM_UNAVAILABLE"
    CLAIM_LOST = "CLAIM_LOST"
    ERROR = "ERROR"


@dataclass(frozen=True)
class RunWorkerOutcome:
    run_id: str
    status: RunWorkerStatus
    advances: int
    last_stage: RunStage | None = None
    run_status: RunStatus | None = None
    error: str | None = None


@dataclass(frozen=True)
class RunWorkerScanResult:
    candidate_count: int
    outcomes: tuple[RunWorkerOutcome, ...]

    @property
    def failures(self) -> tuple[RunWorkerOutcome, ...]:
        return tuple(
            item
            for item in self.outcomes
            if item.status
            in {
                RunWorkerStatus.ESCALATED,
                RunWorkerStatus.FAILED,
                RunWorkerStatus.ERROR,
            }
        )


RunCandidateLoader = Callable[[], tuple[RunRecord, ...]]
RuleScopeLoader = Callable[[RunRecord], WorkerRuleSelection | None]
BlockedRunResolver = Callable[[RunRecord], bool]


class RunWorkerClaimCoordinator(Protocol):
    def acquire(self, run_id: str) -> RunWorkerClaim | None: ...

    def is_owned(self, claim: RunWorkerClaim) -> bool: ...

    def release(self, claim: RunWorkerClaim) -> bool: ...


def make_worker_owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"


class BackgroundRunWorkerService:
    """Advance durable Runs while preserving the existing human-review gate.

    A worker pass may drive a PENDING/RUNNING Run through several durable stages,
    but always yields immediately after the Run first reaches REVIEW_REQUIRED.
    A later pass may enter COMMIT only through ``ExternalAdvanceRunService``, so
    the worker cannot bypass or synthesize human decisions.
    """

    def __init__(
        self,
        advance_service: ExternalAdvanceRunService,
        candidate_loader: RunCandidateLoader,
        rule_scope_loader: RuleScopeLoader,
        blocked_run_resolver: BlockedRunResolver,
        *,
        max_advances_per_run: int = 8,
        claim_coordinator: RunWorkerClaimCoordinator | None = None,
    ) -> None:
        if max_advances_per_run <= 0:
            raise ValueError("max_advances_per_run must be positive")
        self._advance = advance_service
        self._candidate_loader = candidate_loader
        self._rule_scope_loader = rule_scope_loader
        self._resolve_blocked = blocked_run_resolver
        self._max_advances_per_run = max_advances_per_run
        self._claims = claim_coordinator

    def scan_once(self) -> RunWorkerScanResult:
        candidates = self._candidate_loader()
        outcomes: list[RunWorkerOutcome] = []
        for candidate in candidates:
            claim: RunWorkerClaim | None = None
            try:
                if self._claims is not None:
                    claim = self._claims.acquire(candidate.run_id)
                    if claim is None:
                        outcomes.append(
                            RunWorkerOutcome(
                                run_id=candidate.run_id,
                                status=RunWorkerStatus.CLAIM_UNAVAILABLE,
                                advances=0,
                                run_status=candidate.status,
                            ),
                        )
                        continue
                outcomes.append(self._process(candidate, claim))
            except Exception as error:  # noqa: BLE001 - isolate one Run from the runtime loop.
                outcomes.append(
                    RunWorkerOutcome(
                        run_id=candidate.run_id,
                        status=RunWorkerStatus.ERROR,
                        advances=0,
                        run_status=candidate.status,
                        error=f"{type(error).__name__}: {error}",
                    ),
                )
            finally:
                if claim is not None and self._claims is not None:
                    try:
                        self._claims.release(claim)
                    except Exception:  # noqa: BLE001 - session close/restart releases advisory ownership.
                        _LOG.warning(
                            "worker claim release failed run=%s owner=%s",
                            claim.run_id,
                            claim.owner_id,
                            exc_info=True,
                        )
        return RunWorkerScanResult(len(candidates), tuple(outcomes))

    def _process(
        self,
        candidate: RunRecord,
        claim: RunWorkerClaim | None,
    ) -> RunWorkerOutcome:
        if not self._claim_is_owned(claim):
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.CLAIM_LOST,
                advances=0,
                run_status=candidate.status,
            )
        if candidate.status is RunStatus.REVIEW_REQUIRED:
            return self._advance_review_required(candidate, claim)

        if candidate.status not in {RunStatus.PENDING, RunStatus.RUNNING}:
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.ERROR,
                advances=0,
                run_status=candidate.status,
                error=f"unexpected worker candidate status {candidate.status.value}",
            )

        if candidate.blocked_on_run_id is not None and not self._resolve_blocked(candidate):
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.WAITING_UPSTREAM,
                advances=0,
                run_status=candidate.status,
            )

        command = self._command_for(candidate)
        last_stage: RunStage | None = None
        last_status = candidate.status
        for advance_number in range(1, self._max_advances_per_run + 1):
            if not self._claim_is_owned(claim):
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.CLAIM_LOST,
                    advances=advance_number - 1,
                    last_stage=last_stage,
                    run_status=last_status,
                )
            try:
                result = self._advance.advance(command)
            except ExternalHumanReviewRequiredError:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.WAITING_REVIEW,
                    advances=advance_number - 1,
                    last_stage=last_stage,
                    run_status=RunStatus.REVIEW_REQUIRED,
                )
            except RunWorkflowAwaitingUpstream as error:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.WAITING_UPSTREAM,
                    advances=advance_number - 1,
                    last_stage=last_stage,
                    run_status=last_status,
                    error=str(error),
                )
            except RunWorkflowOperationInProgress as error:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.IN_PROGRESS,
                    advances=advance_number - 1,
                    last_stage=last_stage,
                    run_status=last_status,
                    error=str(error),
                )
            except RunWorkflowStepEscalatedError as error:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.ESCALATED,
                    advances=advance_number - 1,
                    last_stage=error.stage,
                    run_status=last_status,
                    error=f"{error.error_code}: {error}",
                )
            except RunWorkflowRunFailedError as error:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.FAILED,
                    advances=advance_number - 1,
                    last_stage=error.stage,
                    run_status=RunStatus.FAILED,
                    error=f"{error.error_code}: {error}",
                )

            last_stage = result.stage
            last_status = result.run_status
            if result.outcome is RunWorkflowOutcome.RETRY_SCHEDULED:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.RETRY_SCHEDULED,
                    advances=advance_number,
                    last_stage=result.stage,
                    run_status=result.run_status,
                )
            if result.run_status is RunStatus.REVIEW_REQUIRED:
                # Never enter COMMIT in the same pass that first reaches the
                # review gate. A later pass re-checks review readiness through
                # ExternalAdvanceRunService.
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.REACHED_REVIEW,
                    advances=advance_number,
                    last_stage=result.stage,
                    run_status=result.run_status,
                )
            if result.run_status is RunStatus.COMPLETED:
                return RunWorkerOutcome(
                    candidate.run_id,
                    RunWorkerStatus.COMPLETED,
                    advances=advance_number,
                    last_stage=result.stage,
                    run_status=result.run_status,
                )

            # Rule scope is only needed on the first pending-stage advance.
            command = RunWorkflowCommand(candidate.run_id)

        return RunWorkerOutcome(
            candidate.run_id,
            RunWorkerStatus.YIELDED,
            advances=self._max_advances_per_run,
            last_stage=last_stage,
            run_status=last_status,
        )

    def _advance_review_required(
        self,
        candidate: RunRecord,
        claim: RunWorkerClaim | None,
    ) -> RunWorkerOutcome:
        if not self._claim_is_owned(claim):
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.CLAIM_LOST,
                advances=0,
                run_status=RunStatus.REVIEW_REQUIRED,
            )
        try:
            result = self._advance.advance(RunWorkflowCommand(candidate.run_id))
        except ExternalHumanReviewRequiredError:
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.WAITING_REVIEW,
                advances=0,
                run_status=RunStatus.REVIEW_REQUIRED,
            )
        except RunWorkflowOperationInProgress as error:
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.IN_PROGRESS,
                advances=0,
                run_status=RunStatus.REVIEW_REQUIRED,
                error=str(error),
            )
        except RunWorkflowStepEscalatedError as error:
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.ESCALATED,
                advances=0,
                last_stage=error.stage,
                run_status=RunStatus.REVIEW_REQUIRED,
                error=f"{error.error_code}: {error}",
            )
        except RunWorkflowRunFailedError as error:
            return RunWorkerOutcome(
                candidate.run_id,
                RunWorkerStatus.FAILED,
                advances=0,
                last_stage=error.stage,
                run_status=RunStatus.FAILED,
                error=f"{error.error_code}: {error}",
            )

        if result.outcome is RunWorkflowOutcome.RETRY_SCHEDULED:
            status = RunWorkerStatus.RETRY_SCHEDULED
        elif result.run_status is RunStatus.COMPLETED:
            status = RunWorkerStatus.COMPLETED
        else:
            status = RunWorkerStatus.YIELDED
        return RunWorkerOutcome(
            candidate.run_id,
            status,
            advances=1,
            last_stage=result.stage,
            run_status=result.run_status,
        )

    def _claim_is_owned(self, claim: RunWorkerClaim | None) -> bool:
        if claim is None or self._claims is None:
            return True
        return self._claims.is_owned(claim)

    def _command_for(self, candidate: RunRecord) -> RunWorkflowCommand:
        if candidate.status is not RunStatus.PENDING:
            return RunWorkflowCommand(candidate.run_id)
        selection = self._rule_scope_loader(candidate)
        if selection is None:
            return RunWorkflowCommand(candidate.run_id)
        return RunWorkflowCommand(
            candidate.run_id,
            source_rule_set_ids=selection.source_rule_set_ids,
            source_rule_ids=selection.source_rule_ids,
        )


def postgres_worker_candidate_loader(
    connection_pool: ConnectionPool,
    *,
    limit: int,
) -> RunCandidateLoader:
    if limit <= 0:
        raise ValueError("limit must be positive")

    def load() -> tuple[RunRecord, ...]:
        with connection_pool.connection() as connection:
            records = RunRepository(connection).list_worker_candidates(limit=limit)
            connection.commit()
        return tuple(records)

    return load


def postgres_worker_rule_scope_loader(connection_pool: ConnectionPool) -> RuleScopeLoader:
    def load(run: RunRecord) -> WorkerRuleSelection | None:
        with connection_pool.connection() as connection:
            if RunRuleScopeRepository(connection).get_scope(run.run_id) is not None:
                connection.commit()
                return None
            repository = RuleSetRepository(connection)
            rule_sets = repository.list_latest_rule_sets_for_pile(run.pile_id)
            rule_ids = tuple(
                rule.rule_id
                for rule_set in rule_sets
                for rule in repository.list_rules_for_rule_set(rule_set.rule_set_id)
            )
            connection.commit()
        return WorkerRuleSelection(
            source_rule_set_ids=tuple(item.rule_set_id for item in rule_sets),
            source_rule_ids=rule_ids,
        )

    return load


def postgres_blocked_run_resolver(connection_pool: ConnectionPool) -> BlockedRunResolver:
    def resolve(candidate: RunRecord) -> bool:
        if candidate.blocked_on_run_id is None:
            return True
        with connection_pool.connection() as connection:
            runs = RunRepository(connection)
            current = runs.get_for_update(candidate.run_id)
            if current is None:
                raise RuntimeError(f"run {candidate.run_id} disappeared")
            if current.blocked_on_run_id is None:
                connection.commit()
                return True
            if current.run_type is not RunType.UPDATE or current.status is not RunStatus.PENDING:
                raise RuntimeError("only a pending UPDATE may carry blocked_on_run_id")
            parent = runs.get_for_update(current.blocked_on_run_id)
            if parent is None or parent.pile_id != current.pile_id:
                raise RuntimeError("blocked UPDATE has an invalid parent Run")
            if parent.status is not RunStatus.COMPLETED:
                connection.commit()
                return False
            updated = runs.resolve_blocked_update_baseline(current.run_id, parent.run_id)
            if updated is None:
                raise RuntimeError("blocked UPDATE lineage changed during resolution")
            connection.commit()
            return True

    return resolve


def run_forever(
    service: BackgroundRunWorkerService,
    *,
    poll_interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> None:
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")
    stopper = stop_event or threading.Event()
    while not stopper.is_set():
        try:
            result = service.scan_once()
        except Exception:  # noqa: BLE001 - keep the daemon alive across transient DB/runtime faults.
            _LOG.exception("worker scan failed; retrying after poll interval")
            stopper.wait(poll_interval_seconds)
            continue
        for outcome in result.outcomes:
            if outcome.status in {
                RunWorkerStatus.ESCALATED,
                RunWorkerStatus.FAILED,
                RunWorkerStatus.ERROR,
            }:
                _LOG.error(
                    "worker %s run=%s stage=%s advances=%s error=%s",
                    outcome.status.value.lower(),
                    outcome.run_id,
                    outcome.last_stage.value if outcome.last_stage is not None else None,
                    outcome.advances,
                    outcome.error,
                )
            elif outcome.status not in {
                RunWorkerStatus.WAITING_REVIEW,
                RunWorkerStatus.WAITING_UPSTREAM,
                RunWorkerStatus.IN_PROGRESS,
                RunWorkerStatus.CLAIM_UNAVAILABLE,
                RunWorkerStatus.CLAIM_LOST,
            }:
                _LOG.info(
                    "worker %s run=%s stage=%s advances=%s run_status=%s",
                    outcome.status.value.lower(),
                    outcome.run_id,
                    outcome.last_stage.value if outcome.last_stage is not None else None,
                    outcome.advances,
                    outcome.run_status.value if outcome.run_status is not None else None,
                )
        stopper.wait(poll_interval_seconds)


def main() -> None:
    from api.external_dependencies import get_external_advance_run_service
    from config.settings import get_settings
    from db.pool import pool

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    service = BackgroundRunWorkerService(
        get_external_advance_run_service(),
        postgres_worker_candidate_loader(pool, limit=settings.worker_batch_size),
        postgres_worker_rule_scope_loader(pool),
        postgres_blocked_run_resolver(pool),
        max_advances_per_run=settings.worker_max_advances_per_run,
        claim_coordinator=PostgresRunWorkerClaimCoordinator(
            settings.database_url,
            owner_id=make_worker_owner_id(),
        ),
    )
    pool.open()
    try:
        run_forever(service, poll_interval_seconds=settings.worker_poll_interval_seconds)
    except KeyboardInterrupt:
        _LOG.info("worker stopped")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
