from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from api.interface_contract import MCP_TOOL_BY_OPERATION
from config.settings import get_settings

from .support import external_e2e_harness


def _run(coro):
    return asyncio.run(coro)


async def _call(client: Client, name: str, arguments: dict) -> object:
    result = await client.call_tool(name, arguments)
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    structured = result.structured_content
    if set(structured) == {"result"}:
        return structured["result"]
    return structured


async def _drive_mcp(server) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == set(MCP_TOOL_BY_OPERATION.values())

        pile = await _call(
            client,
            "create_pile",
            {
                "name": "MCP black-box pile",
                "description": "Phase 6B-6",
                "watch_enabled": False,
            },
        )
        assert isinstance(pile, dict)
        pile_id = pile["pile_id"]

        rule_set = await _call(
            client,
            "create_rule_set",
            {
                "pile_id": pile_id,
                "name": "MCP E2E rules",
                "version": 1,
                "rules": [
                    {
                        "rule_key": "payment-term",
                        "title": "Payment term maximum",
                        "description": "Exercise machine-driven human gate",
                        "severity": "HIGH",
                        "definition": {"max_days": 45},
                    },
                    {
                        "rule_key": "secondary-check",
                        "title": "Secondary check",
                        "description": None,
                        "severity": "MEDIUM",
                        "definition": {"required": True},
                    },
                ],
            },
        )
        assert isinstance(rule_set, dict)
        rule_set_id = rule_set["rule_set_id"]
        rule_ids = [item["rule_id"] for item in rule_set["rules"]]

        source = b"MCP Agreement\nPayment term is 60 days.\nEnd.\n"
        ingestion = await _call(
            client,
            "ingest_document",
            {
                "pile_id": pile_id,
                "logical_key": "mcp-contract",
                "filename": "mcp-contract.txt",
                "media_type": "text/plain",
                "source_base64": base64.b64encode(source).decode("ascii"),
            },
        )
        assert isinstance(ingestion, dict)
        run_id = ingestion["created_run_ids"][0]

        for expected_stage in ("INGEST", "UNDERSTAND", "EXAMINE", "REVIEW"):
            advanced = await _call(
                client,
                "advance_run",
                {
                    "run_id": run_id,
                    "source_rule_set_ids": [rule_set_id],
                    "source_rule_ids": rule_ids,
                },
            )
            assert isinstance(advanced, dict)
            assert advanced["stage"] == expected_stage

        blocked = await client.call_tool(
            "advance_run",
            {
                "run_id": run_id,
                "source_rule_set_ids": [rule_set_id],
                "source_rule_ids": rule_ids,
            },
        )
        assert blocked.is_error is True
        assert "human review" in blocked.content[0].text.lower()

        review = await _call(client, "get_run_review", {"run_id": run_id})
        assert isinstance(review, dict)
        assert review["commit_ready"] is False
        assert len(review["findings"]) == 2
        for index, finding in enumerate(review["findings"]):
            decision = "APPROVED" if index == 0 else "REJECTED"
            recorded = await _call(
                client,
                "review_finding",
                {
                    "run_id": run_id,
                    "finding_id": finding["finding_id"],
                    "decision": decision,
                    "actor_type": "HUMAN",
                    "actor_id": "mcp-e2e@example.test",
                    "idempotency_key": f"review:{run_id}:{finding['finding_id']}",
                },
            )
            assert isinstance(recorded, dict)
            assert recorded["decision"] == decision

        ready = await _call(client, "get_run_review", {"run_id": run_id})
        assert isinstance(ready, dict)
        assert ready["review_complete"] is True
        assert ready["commit_ready"] is True

        committed = await _call(
            client,
            "advance_run",
            {
                "run_id": run_id,
                "source_rule_set_ids": [rule_set_id],
                "source_rule_ids": rule_ids,
            },
        )
        assert isinstance(committed, dict)
        assert committed["stage"] == "COMMIT"
        assert committed["run_status"] == "COMPLETED"

        report = await _call(client, "get_run_report", {"run_id": run_id})
        assert isinstance(report, dict)
        assert report["run_id"] == run_id
        assert "# Final Report" in report["content_markdown"]
        metrics = await _call(client, "get_run_metrics", {"run_id": run_id})
        assert isinstance(metrics, list)
        assert {item["stage"] for item in metrics} == {
            "INGEST",
            "UNDERSTAND",
            "EXAMINE",
            "REVIEW",
            "COMMIT",
        }


def test_mcp_client_drives_the_full_human_gated_flow_without_http() -> None:
    with external_e2e_harness("task1_phase6b_mcp") as harness:
        _run(_drive_mcp(harness.mcp_server))


async def _stdio_discovery() -> set[str]:
    root = Path(__file__).resolve().parents[2]
    environment = {
        "PYTHONPATH": str(root / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "TASK1_MCP_TRANSPORT": "stdio",
        "DATABASE_URL": get_settings().database_url,
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "api.mcp"],
        env=environment,
        cwd=root,
    )
    async with Client(stdio_client(parameters)) as client:
        tools = await client.list_tools()
        return {tool.name for tool in tools.tools}


def test_real_stdio_mcp_transport_starts_and_discovers_the_locked_tools() -> None:
    assert _run(_stdio_discovery()) == set(MCP_TOOL_BY_OPERATION.values())
