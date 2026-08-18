from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from api.application.external_advance_run_service import ExternalHumanReviewRequiredError
from api.application.run_workflow_service import RunWorkflowCommand, RunWorkflowOutcome
from domain.run import RunStage, RunStatus, RunType
from repositories.run_repository import RunRecord
from runtime.run_worker import (
    BackgroundRunWorkerService,
    RunWorkerScanResult,
    RunWorkerStatus,
    WorkerRuleSelection,
    run_forever,
)
from runtime.run_worker_claims import RunWorkerClaim


@dataclass(frozen=True)
class _AdvanceResult:
    stage: RunStage
    run_status: RunStatus
    outcome: RunWorkflowOutcome = RunWorkflowOutcome.COMPLETED


class _AdvanceService:
    def __init__(self, responses: dict[str, list[object]]) -> None:
        self.responses = responses
        self.commands: list[RunWorkflowCommand] = []

    def advance(self, command: RunWorkflowCommand):
        self.commands.append(command)
        response = self.responses[command.run_id].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _run(
    run_id: str,
    *,
    status: RunStatus = RunStatus.PENDING,
    blocked_on_run_id: str | None = None,
) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        run_id=run_id,
        pile_id="11111111-1111-4111-8111-111111111111",
        run_type=RunType.UPDATE if blocked_on_run_id is not None else RunType.INITIAL,
        status=status,
        trigger_document_version_id=(
            "22222222-2222-4222-8222-222222222222"
            if blocked_on_run_id is not None
            else None
        ),
        parent_run_id=blocked_on_run_id,
        baseline_run_id=None,
        blocked_on_run_id=blocked_on_run_id,
        commit_required=True,
        started_at=None,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )


def _service(
    advance: _AdvanceService,
    candidates: tuple[RunRecord, ...],
    *,
    selection: WorkerRuleSelection | None = None,
    resolve_blocked: bool = True,
    max_advances_per_run: int = 8,
    claim_coordinator=None,
) -> BackgroundRunWorkerService:
    return BackgroundRunWorkerService(
        advance,  # type: ignore[arg-type]
        lambda: candidates,
        lambda _run: selection,
        lambda _run: resolve_blocked,
        max_advances_per_run=max_advances_per_run,
        claim_coordinator=claim_coordinator,
    )


def test_worker_drives_pending_run_to_review_but_never_commits_in_same_pass() -> None:
    candidate = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    advance = _AdvanceService(
        {
            candidate.run_id: [
                _AdvanceResult(RunStage.INGEST, RunStatus.RUNNING),
                _AdvanceResult(RunStage.UNDERSTAND, RunStatus.RUNNING),
                _AdvanceResult(RunStage.EXAMINE, RunStatus.RUNNING),
                _AdvanceResult(RunStage.REVIEW, RunStatus.REVIEW_REQUIRED),
                _AdvanceResult(RunStage.COMMIT, RunStatus.COMPLETED),
            ],
        },
    )
    selection = WorkerRuleSelection(
        ("33333333-3333-4333-8333-333333333333",),
        ("44444444-4444-4444-8444-444444444444",),
    )

    result = _service(advance, (candidate,), selection=selection).scan_once()

    assert result.candidate_count == 1
    assert result.outcomes[0].status is RunWorkerStatus.REACHED_REVIEW
    assert result.outcomes[0].advances == 4
    assert len(advance.commands) == 4
    assert advance.commands[0].source_rule_set_ids == selection.source_rule_set_ids
    assert advance.commands[0].source_rule_ids == selection.source_rule_ids
    assert all(command.source_rule_set_ids is None for command in advance.commands[1:])
    assert len(advance.responses[candidate.run_id]) == 1


def test_worker_review_required_waits_when_external_human_gate_is_not_ready() -> None:
    candidate = _run(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab",
        status=RunStatus.REVIEW_REQUIRED,
    )
    advance = _AdvanceService(
        {candidate.run_id: [ExternalHumanReviewRequiredError("human review required")]},
    )

    result = _service(advance, (candidate,)).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.WAITING_REVIEW
    assert result.outcomes[0].advances == 0
    assert len(advance.commands) == 1


def test_worker_commits_review_ready_run_on_later_pass() -> None:
    candidate = _run(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaac",
        status=RunStatus.REVIEW_REQUIRED,
    )
    advance = _AdvanceService(
        {candidate.run_id: [_AdvanceResult(RunStage.COMMIT, RunStatus.COMPLETED)]},
    )

    result = _service(advance, (candidate,)).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.COMPLETED
    assert result.outcomes[0].advances == 1
    assert result.outcomes[0].last_stage is RunStage.COMMIT


def test_worker_does_not_touch_blocked_update_until_parent_is_completed() -> None:
    candidate = _run(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaad",
        blocked_on_run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )
    advance = _AdvanceService({candidate.run_id: []})

    result = _service(advance, (candidate,), resolve_blocked=False).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.WAITING_UPSTREAM
    assert advance.commands == []


def test_worker_can_capture_an_intentionally_empty_rule_scope() -> None:
    candidate = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaae")
    advance = _AdvanceService(
        {candidate.run_id: [_AdvanceResult(RunStage.REVIEW, RunStatus.REVIEW_REQUIRED)]},
    )

    result = _service(
        advance,
        (candidate,),
        selection=WorkerRuleSelection((), ()),
    ).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.REACHED_REVIEW
    assert advance.commands[0].source_rule_set_ids == ()
    assert advance.commands[0].source_rule_ids == ()


def test_worker_stops_when_workflow_schedules_a_retry() -> None:
    candidate = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaf")
    advance = _AdvanceService(
        {
            candidate.run_id: [
                _AdvanceResult(
                    RunStage.UNDERSTAND,
                    RunStatus.RUNNING,
                    RunWorkflowOutcome.RETRY_SCHEDULED,
                ),
            ],
        },
    )

    result = _service(
        advance,
        (candidate,),
        selection=WorkerRuleSelection((), ()),
    ).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.RETRY_SCHEDULED
    assert result.outcomes[0].advances == 1


def test_worker_yields_after_configured_advance_budget() -> None:
    candidate = _run(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaba",
        status=RunStatus.RUNNING,
    )
    advance = _AdvanceService(
        {
            candidate.run_id: [
                _AdvanceResult(RunStage.UNDERSTAND, RunStatus.RUNNING),
                _AdvanceResult(RunStage.EXAMINE, RunStatus.RUNNING),
            ],
        },
    )

    result = _service(
        advance,
        (candidate,),
        max_advances_per_run=2,
    ).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.YIELDED
    assert result.outcomes[0].advances == 2


def test_worker_isolates_one_run_failure_and_continues_with_next_candidate() -> None:
    broken = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaabb")
    ready = _run(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaabc",
        status=RunStatus.REVIEW_REQUIRED,
    )
    advance = _AdvanceService(
        {
            broken.run_id: [RuntimeError("boom")],
            ready.run_id: [_AdvanceResult(RunStage.COMMIT, RunStatus.COMPLETED)],
        },
    )

    result = _service(
        advance,
        (broken, ready),
        selection=WorkerRuleSelection((), ()),
    ).scan_once()

    assert [item.status for item in result.outcomes] == [
        RunWorkerStatus.ERROR,
        RunWorkerStatus.COMPLETED,
    ]
    assert "RuntimeError: boom" in (result.outcomes[0].error or "")


class _ClaimCoordinator:
    def __init__(
        self,
        *,
        acquire: bool = True,
        owned_results: list[bool] | None = None,
    ) -> None:
        self.acquire_enabled = acquire
        self.owned_results = owned_results or []
        self.acquired: list[str] = []
        self.checked: list[str] = []
        self.released: list[str] = []

    def acquire(self, run_id: str) -> RunWorkerClaim | None:
        self.acquired.append(run_id)
        if not self.acquire_enabled:
            return None
        return RunWorkerClaim(
            run_id=run_id,
            owner_id="unit-worker",
            lock_key=1,
            connection=None,
        )  # type: ignore[arg-type]

    def is_owned(self, claim: RunWorkerClaim) -> bool:
        self.checked.append(claim.run_id)
        if self.owned_results:
            return self.owned_results.pop(0)
        return True

    def release(self, claim: RunWorkerClaim) -> bool:
        self.released.append(claim.run_id)
        return True


def test_worker_skips_candidate_claimed_by_another_process() -> None:
    candidate = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac1")
    advance = _AdvanceService({candidate.run_id: []})
    claims = _ClaimCoordinator(acquire=False)

    result = _service(
        advance,
        (candidate,),
        selection=WorkerRuleSelection((), ()),
        claim_coordinator=claims,
    ).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.CLAIM_UNAVAILABLE
    assert result.outcomes[0].advances == 0
    assert advance.commands == []
    assert claims.released == []


def test_worker_stops_if_its_postgres_claim_session_is_lost_between_stages() -> None:
    candidate = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac2")
    advance = _AdvanceService(
        {
            candidate.run_id: [
                _AdvanceResult(RunStage.INGEST, RunStatus.RUNNING),
                _AdvanceResult(RunStage.UNDERSTAND, RunStatus.RUNNING),
            ],
        },
    )
    claims = _ClaimCoordinator(owned_results=[True, True, False])

    result = _service(
        advance,
        (candidate,),
        selection=WorkerRuleSelection((), ()),
        claim_coordinator=claims,
    ).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.CLAIM_LOST
    assert result.outcomes[0].advances == 1
    assert len(advance.commands) == 1
    assert claims.released == [candidate.run_id]


def test_worker_releases_claim_after_reaching_review_gate() -> None:
    candidate = _run("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac3")
    advance = _AdvanceService(
        {candidate.run_id: [_AdvanceResult(RunStage.REVIEW, RunStatus.REVIEW_REQUIRED)]},
    )
    claims = _ClaimCoordinator()

    result = _service(
        advance,
        (candidate,),
        selection=WorkerRuleSelection((), ()),
        claim_coordinator=claims,
    ).scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.REACHED_REVIEW
    assert claims.acquired == [candidate.run_id]
    assert claims.released == [candidate.run_id]


def test_worker_restart_resumes_running_candidate_without_recapturing_scope() -> None:
    candidate = _run(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaac4",
        status=RunStatus.RUNNING,
    )
    advance = _AdvanceService(
        {candidate.run_id: [_AdvanceResult(RunStage.REVIEW, RunStatus.REVIEW_REQUIRED)]},
    )
    scope_calls = 0

    def scope_loader(_run: RunRecord):
        nonlocal scope_calls
        scope_calls += 1
        return WorkerRuleSelection((), ())

    service = BackgroundRunWorkerService(
        advance,  # type: ignore[arg-type]
        lambda: (candidate,),
        scope_loader,
        lambda _run: True,
    )

    result = service.scan_once()

    assert result.outcomes[0].status is RunWorkerStatus.REACHED_REVIEW
    assert scope_calls == 0


def test_worker_daemon_retries_after_transient_scan_failure() -> None:
    stop = threading.Event()

    class _RestartingService:
        calls = 0

        def scan_once(self) -> RunWorkerScanResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("database restarting")
            stop.set()
            return RunWorkerScanResult(0, ())

    service = _RestartingService()
    run_forever(service, poll_interval_seconds=0.001, stop_event=stop)  # type: ignore[arg-type]

    assert service.calls == 2
