"""Task-1 MCP server exposing the same application use cases as REST."""

from __future__ import annotations

import base64
import binascii
import os
from io import BytesIO

from mcp.server import MCPServer

from api.application.acknowledge_run_review_service import RunReviewAcknowledgementCommand
from api.application.create_pile_service import CreatePileCommand
from api.application.create_rule_set_service import CreateRuleCommand, CreateRuleSetCommand
from api.application.external_update_review_service import ExternalUpdateReviewCommand
from api.application.ingest_and_reconcile_document_service import IngestAndReconcileDocumentCommand
from api.application.review_decision_service import ReviewDecisionCommand
from api.application.run_workflow_service import RunWorkflowCommand
from api.change_history_presenter import change_history_response
from api.interface_contract import MCP_TOOL_BY_OPERATION, ExternalOperation
from api.mcp.presenters import (
    advance_response,
    fact_response,
    finding_decision_response,
    finding_response,
    ingest_response,
    metric_response,
    pile_response,
    review_response,
    rule_response,
    run_response,
    run_review_acknowledgement_response,
    update_decision_response,
)
from api.mcp.schemas import McpFinalReportResult, McpRuleInput
from api.mcp.service_provider import McpServiceProvider, default_mcp_service_provider
from api.rest.schemas.core import (
    AdvanceRunResponse,
    FactResponse,
    FindingResponse,
    IngestDocumentResponse,
    PileResponse,
    RuleSetResponse,
    RunMetricResponse,
    RunResponse,
)
from api.rest.schemas.history import ChangeHistoryRunResponse
from api.rest.schemas.review import (
    FindingDecisionResponse,
    RunReviewAcknowledgementResponse,
    RunReviewResponse,
    UpdateDecisionResponse,
)
from domain.review_decision import ReviewDecisionType
from domain.update_review import UpdateReviewDecisionType

_SERVER_NAME = "task1-document-analysis"
_SERVER_VERSION = "0.1.0"
_SERVER_INSTRUCTIONS = (
    "Drive the durable Task-1 document workflow. Use advance_run to progress stages. "
    "When a run reaches REVIEW_REQUIRED, inspect get_run_review and explicitly record "
    "the required human decisions before calling advance_run again. Final output is only "
    "created by the durable COMMIT stage; no MCP tool bypasses that gate. Use the "
    "change-history tools to explain completed UPDATEs from committed provenance."
)
_ALLOWED_TRANSPORTS = {"stdio", "streamable-http", "sse"}


def _tool_name(operation: ExternalOperation) -> str:
    return MCP_TOOL_BY_OPERATION[operation]


def _decode_source(source_base64: str) -> bytes:
    if not isinstance(source_base64, str) or not source_base64:
        raise ValueError("source_base64 must be a non-empty base64 string")
    try:
        return base64.b64decode(source_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("source_base64 must contain valid canonical base64") from error


def create_mcp_server(
    service_provider: McpServiceProvider | None = None,
) -> MCPServer:
    """Build the MCP server with lazy production wiring or injected test services."""

    services = service_provider or default_mcp_service_provider()
    server = MCPServer(
        _SERVER_NAME,
        version=_SERVER_VERSION,
        instructions=_SERVER_INSTRUCTIONS,
    )

    @server.tool(name=_tool_name(ExternalOperation.CREATE_PILE))
    def create_pile(
        name: str,
        description: str | None = None,
        watch_enabled: bool = False,
        watch_location: str | None = None,
    ) -> PileResponse:
        """Create one durable document pile."""

        item = services.create_pile().create(
            CreatePileCommand(
                name=name,
                description=description,
                watch_enabled=watch_enabled,
                watch_location=watch_location,
            ),
        )
        return pile_response(item)

    @server.tool(name=_tool_name(ExternalOperation.GET_PILE))
    def get_pile(pile_id: str) -> PileResponse:
        """Read one pile including its authoritative latest run pointer."""

        return pile_response(services.query().get_pile(pile_id))

    @server.tool(name=_tool_name(ExternalOperation.CREATE_RULE_SET))
    def create_rule_set(
        pile_id: str,
        name: str,
        version: int,
        rules: list[McpRuleInput],
        description: str | None = None,
    ) -> RuleSetResponse:
        """Create one immutable rule-set version and its rules atomically."""

        result = services.create_rule_set().create(
            CreateRuleSetCommand(
                pile_id=pile_id,
                name=name,
                description=description,
                version=version,
                rules=tuple(
                    CreateRuleCommand(
                        rule_key=rule.rule_key,
                        title=rule.title,
                        description=rule.description,
                        severity=rule.severity,
                        definition=rule.definition,
                    )
                    for rule in rules
                ),
            ),
        )
        return RuleSetResponse(
            **result.rule_set.__dict__,
            rules=[rule_response(rule) for rule in result.rules],
        )

    @server.tool(name=_tool_name(ExternalOperation.INGEST_DOCUMENT))
    def ingest_document(
        pile_id: str,
        logical_key: str,
        filename: str,
        media_type: str,
        source_base64: str,
    ) -> IngestDocumentResponse:
        """Ingest one PDF/DOCX/TXT source encoded as base64 and reconcile Run lineage."""

        if not filename.strip():
            raise ValueError("filename must be non-empty")
        if not media_type.strip():
            raise ValueError("media_type must be non-empty")
        source = _decode_source(source_base64)
        result = services.ingestion().ingest(
            IngestAndReconcileDocumentCommand(
                pile_id=pile_id,
                logical_key=logical_key,
                source=BytesIO(source),
            ),
        )
        return ingest_response(result)

    @server.tool(name=_tool_name(ExternalOperation.LIST_PILE_RUNS))
    def list_pile_runs(pile_id: str, limit: int = 100) -> list[RunResponse]:
        """List durable runs for a pile, including complete stage-attempt history."""

        return [run_response(item) for item in services.query().list_runs(pile_id, limit=limit)]

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN))
    def get_run(run_id: str) -> RunResponse:
        """Read one durable Run and all of its stage attempts."""

        return run_response(services.query().get_run(run_id))

    @server.tool(name=_tool_name(ExternalOperation.ADVANCE_RUN))
    def advance_run(
        run_id: str,
        source_rule_set_ids: list[str] | None = None,
        source_rule_ids: list[str] | None = None,
    ) -> AdvanceRunResponse:
        """Advance exactly one durable workflow stage; never bypasses human review."""

        result = services.advance().advance(
            RunWorkflowCommand(
                run_id=run_id,
                source_rule_set_ids=(
                    tuple(source_rule_set_ids) if source_rule_set_ids is not None else None
                ),
                source_rule_ids=(tuple(source_rule_ids) if source_rule_ids is not None else None),
            ),
        )
        return advance_response(result)

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN_FACTS))
    def get_run_facts(run_id: str, limit: int = 100) -> list[FactResponse]:
        """Read run-local facts with exact evidence provenance."""

        return [
            fact_response(item)
            for item in services.query().list_run_facts(run_id, limit=limit)
        ]

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN_FINDINGS))
    def get_run_findings(run_id: str, limit: int = 100) -> list[FindingResponse]:
        """Read run-local findings with exact evidence provenance."""

        return [
            finding_response(item)
            for item in services.query().list_run_findings(run_id, limit=limit)
        ]

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN_REVIEW))
    def get_run_review(run_id: str) -> RunReviewResponse:
        """Read the complete authoritative review payload and commit readiness."""

        return review_response(services.review_query().get_review(run_id))

    @server.tool(name=_tool_name(ExternalOperation.REVIEW_FINDING))
    def review_finding(
        run_id: str,
        finding_id: str,
        decision: ReviewDecisionType,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> FindingDecisionResponse:
        """Approve or reject exactly one finding with durable idempotency."""

        result = services.review_decision().record(
            ReviewDecisionCommand(
                run_id=run_id,
                finding_id=finding_id,
                decision=decision,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            ),
        )
        return finding_decision_response(result)

    @server.tool(name=_tool_name(ExternalOperation.ACKNOWLEDGE_RUN_REVIEW))
    def acknowledge_run_review(
        run_id: str,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
    ) -> RunReviewAcknowledgementResponse:
        """Explicitly acknowledge a zero-finding non-UPDATE human review gate."""

        result = services.acknowledge_run_review().record(
            RunReviewAcknowledgementCommand(
                run_id=run_id,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            ),
        )
        return run_review_acknowledgement_response(result)

    @server.tool(name=_tool_name(ExternalOperation.REVIEW_UPDATE))
    def review_update(
        run_id: str,
        decision: UpdateReviewDecisionType,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
        expected_change_set_fingerprint: str,
        fact_id: str | None = None,
    ) -> UpdateDecisionResponse:
        """Review one material UPDATE Fact item, or a zero-item legacy change set."""

        result = services.update_review().record(
            ExternalUpdateReviewCommand(
                run_id=run_id,
                decision=decision,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                expected_change_set_fingerprint=expected_change_set_fingerprint,
                fact_id=fact_id,
            ),
        )
        return update_decision_response(result)

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN_REPORT))
    def get_run_report(run_id: str) -> McpFinalReportResult:
        """Read verified committed report metadata plus exact Markdown content."""

        report, content = services.review_query().read_report_content(run_id)
        return McpFinalReportResult(
            **report.__dict__,
            content_markdown=content.decode("utf-8"),
        )

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN_METRICS))
    def get_run_metrics(run_id: str, limit: int = 100) -> list[RunMetricResponse]:
        """Read per-stage/per-attempt duration, token, and cost metrics."""

        return [
            metric_response(item)
            for item in services.query().list_run_metrics(run_id, limit=limit)
        ]

    @server.tool(name=_tool_name(ExternalOperation.LIST_PILE_CHANGE_HISTORY))
    def list_pile_change_history(
        pile_id: str,
        limit: int = 100,
    ) -> list[ChangeHistoryRunResponse]:
        """List committed UPDATE history with source, review, and commit provenance."""

        return [
            change_history_response(item)
            for item in services.change_history().list_for_pile(pile_id, limit=limit)
        ]

    @server.tool(name=_tool_name(ExternalOperation.GET_RUN_CHANGE_HISTORY))
    def get_run_change_history(run_id: str) -> ChangeHistoryRunResponse:
        """Explain what one completed UPDATE changed, when, and because of which source."""

        return change_history_response(services.change_history().get_for_run(run_id))

    return server


mcp = create_mcp_server()


def main() -> None:
    """Run the MCP server; stdio is the default local-agent transport."""

    transport = os.environ.get("TASK1_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in _ALLOWED_TRANSPORTS:
        allowed = ", ".join(sorted(_ALLOWED_TRANSPORTS))
        raise ValueError(f"TASK1_MCP_TRANSPORT must be one of: {allowed}")
    mcp.run(transport=transport)


if __name__ == "__main__":  # pragma: no cover - exercised by deployment/Inspector
    main()
