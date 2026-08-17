"""External workflow facade that enforces the human gate before COMMIT starts."""

from __future__ import annotations

from api.application.external_review_query_service import ExternalReviewQueryService
from api.application.run_workflow_service import (
    RunWorkflowCommand,
    RunWorkflowResult,
    RunWorkflowService,
)
from domain.run import RunStatus


class ExternalAdvanceRunError(Exception):
    pass


class ExternalHumanReviewRequiredError(ExternalAdvanceRunError):
    pass


class ExternalAdvanceRunService:
    def __init__(
        self,
        review_query: ExternalReviewQueryService,
        workflow: RunWorkflowService,
    ) -> None:
        self._review_query = review_query
        self._workflow = workflow

    def advance(self, command: RunWorkflowCommand) -> RunWorkflowResult:
        review = self._review_query.get_review(command.run_id)
        if review.run_status is RunStatus.REVIEW_REQUIRED and not review.commit_ready:
            raise ExternalHumanReviewRequiredError(
                "Run cannot enter COMMIT until required human review is complete and approved",
            )
        return self._workflow.advance(command)
