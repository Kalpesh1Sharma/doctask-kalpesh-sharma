from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.application.external_advance_run_service import (
    ExternalAdvanceRunService,
    ExternalHumanReviewRequiredError,
)
from api.application.run_workflow_service import RunWorkflowCommand
from domain.run import RunStatus

_RUN_ID = "11111111-1111-1111-1111-111111111111"


class _ReviewQuery:
    def __init__(self, *, status: RunStatus, commit_ready: bool) -> None:
        self.status = status
        self.commit_ready = commit_ready

    def get_review(self, run_id: str):
        assert run_id == _RUN_ID
        return SimpleNamespace(
            run_status=self.status,
            commit_ready=self.commit_ready,
        )


class _Workflow:
    calls = 0

    def advance(self, command):
        self.calls += 1
        return SimpleNamespace(command=command)


def test_review_required_run_cannot_start_commit_before_human_gate_is_ready() -> None:
    workflow = _Workflow()
    service = ExternalAdvanceRunService(
        _ReviewQuery(status=RunStatus.REVIEW_REQUIRED, commit_ready=False),
        workflow,
    )

    with pytest.raises(ExternalHumanReviewRequiredError):
        service.advance(RunWorkflowCommand(_RUN_ID))

    assert workflow.calls == 0


def test_review_ready_run_delegates_to_authoritative_workflow() -> None:
    workflow = _Workflow()
    service = ExternalAdvanceRunService(
        _ReviewQuery(status=RunStatus.REVIEW_REQUIRED, commit_ready=True),
        workflow,
    )
    command = RunWorkflowCommand(_RUN_ID)

    result = service.advance(command)

    assert workflow.calls == 1
    assert result.command == command


def test_non_review_stage_delegates_without_review_gate() -> None:
    workflow = _Workflow()
    service = ExternalAdvanceRunService(
        _ReviewQuery(status=RunStatus.RUNNING, commit_ready=False),
        workflow,
    )

    service.advance(RunWorkflowCommand(_RUN_ID))

    assert workflow.calls == 1
