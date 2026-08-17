"""Transport-neutral operation identities for the external Task-1 interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExternalOperation(StrEnum):
    CREATE_PILE = "create_pile"
    GET_PILE = "get_pile"
    CREATE_RULE_SET = "create_rule_set"
    INGEST_DOCUMENT = "ingest_document"
    LIST_PILE_RUNS = "list_pile_runs"
    GET_RUN = "get_run"
    ADVANCE_RUN = "advance_run"
    GET_RUN_FACTS = "get_run_facts"
    GET_RUN_FINDINGS = "get_run_findings"
    GET_RUN_REVIEW = "get_run_review"
    REVIEW_FINDING = "review_finding"
    REVIEW_UPDATE = "review_update"
    GET_RUN_REPORT = "get_run_report"
    GET_RUN_METRICS = "get_run_metrics"


@dataclass(frozen=True)
class RestOperationContract:
    operation: ExternalOperation
    method: str
    path: str


REST_OPERATIONS: tuple[RestOperationContract, ...] = (
    RestOperationContract(ExternalOperation.CREATE_PILE, "POST", "/api/v1/piles"),
    RestOperationContract(ExternalOperation.GET_PILE, "GET", "/api/v1/piles/{pile_id}"),
    RestOperationContract(
        ExternalOperation.CREATE_RULE_SET,
        "POST",
        "/api/v1/piles/{pile_id}/rule-sets",
    ),
    RestOperationContract(
        ExternalOperation.INGEST_DOCUMENT,
        "POST",
        "/api/v1/piles/{pile_id}/documents",
    ),
    RestOperationContract(
        ExternalOperation.LIST_PILE_RUNS,
        "GET",
        "/api/v1/piles/{pile_id}/runs",
    ),
    RestOperationContract(ExternalOperation.GET_RUN, "GET", "/api/v1/runs/{run_id}"),
    RestOperationContract(
        ExternalOperation.ADVANCE_RUN,
        "POST",
        "/api/v1/runs/{run_id}/advance",
    ),
    RestOperationContract(
        ExternalOperation.GET_RUN_FACTS,
        "GET",
        "/api/v1/runs/{run_id}/facts",
    ),
    RestOperationContract(
        ExternalOperation.GET_RUN_FINDINGS,
        "GET",
        "/api/v1/runs/{run_id}/findings",
    ),
    RestOperationContract(
        ExternalOperation.GET_RUN_REVIEW,
        "GET",
        "/api/v1/runs/{run_id}/review",
    ),
    RestOperationContract(
        ExternalOperation.REVIEW_FINDING,
        "POST",
        "/api/v1/runs/{run_id}/findings/{finding_id}/decision",
    ),
    RestOperationContract(
        ExternalOperation.REVIEW_UPDATE,
        "POST",
        "/api/v1/runs/{run_id}/update/decision",
    ),
    RestOperationContract(
        ExternalOperation.GET_RUN_REPORT,
        "GET",
        "/api/v1/runs/{run_id}/report",
    ),
    RestOperationContract(
        ExternalOperation.GET_RUN_METRICS,
        "GET",
        "/api/v1/runs/{run_id}/metrics",
    ),
)


MCP_TOOL_BY_OPERATION: dict[ExternalOperation, str] = {
    operation: operation.value for operation in ExternalOperation
}
