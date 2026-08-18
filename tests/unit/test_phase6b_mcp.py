from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from api.application.external_advance_run_service import ExternalHumanReviewRequiredError
from api.external_dependencies import get_change_history_service, get_external_query_service
from api.mcp.contracts import MCP_TOOL_NAMES
from api.mcp.server import create_mcp_server
from api.mcp.service_provider import McpServiceProvider, default_mcp_service_provider
from api.rest.dependencies import (
    get_change_history_service as get_rest_change_history_service,
)
from api.rest.dependencies import (
    get_external_query_service as get_rest_external_query_service,
)

_PILE_ID = "11111111-1111-4111-8111-111111111111"
_RUN_ID = "22222222-2222-4222-8222-222222222222"
_VERSION_ID = "33333333-3333-4333-8333-333333333333"
_DOCUMENT_ID = "44444444-4444-4444-8444-444444444444"
_OUTPUT_ID = "55555555-5555-4555-8555-555555555555"
_NOW = datetime(2026, 8, 16, 18, 0, tzinfo=UTC)


def _unused() -> object:
    raise AssertionError("unexpected MCP service resolution")


def _provider(**overrides: object) -> McpServiceProvider:
    values: dict[str, object] = {
        "create_pile": _unused,
        "create_rule_set": _unused,
        "query": _unused,
        "ingestion": _unused,
        "advance": _unused,
        "review_query": _unused,
        "review_decision": _unused,
        "update_review": _unused,
        "change_history": _unused,
    }
    values.update(overrides)
    return McpServiceProvider(**values)  # type: ignore[arg-type]


class _CreatePile:
    command = None

    def create(self, command):
        self.command = command
        return SimpleNamespace(
            pile_id=_PILE_ID,
            name=command.name,
            description=command.description,
            watch_enabled=command.watch_enabled,
            watch_location=command.watch_location,
            latest_run_id=None,
            created_at=_NOW,
            updated_at=_NOW,
        )


class _Ingestion:
    source_bytes = b""

    def ingest(self, command):
        self.source_bytes = command.source.read()
        return SimpleNamespace(
            document_id=_DOCUMENT_ID,
            document_version_id=_VERSION_ID,
            content_hash="sha256:" + "a" * 64,
            storage_uri="source://sha256/" + "a" * 64,
            replayed=False,
            created_run_ids=(_RUN_ID,),
            reconciled_blocked_run_id=None,
        )


class _AdvanceBlocked:
    def advance(self, _command):
        raise ExternalHumanReviewRequiredError(
            "Run cannot enter COMMIT until required human review is complete and approved",
        )


class _HistoryQuery:
    def list_for_pile(self, pile_id: str, *, limit: int = 100):
        assert pile_id == _PILE_ID
        assert limit == 7
        return (self._history(),)

    def get_for_run(self, run_id: str):
        assert run_id == _RUN_ID
        return self._history()

    @staticmethod
    def _history():
        source = SimpleNamespace(
            pile_id=_PILE_ID,
            document_id=_DOCUMENT_ID,
            logical_key="closing-disclosure.pdf",
            document_version_id=_VERSION_ID,
            version_number=2,
            content_hash="sha256:" + "d" * 64,
            mime_type="application/pdf",
            role="CLOSING_DISCLOSURE",
        )
        review = SimpleNamespace(
            decision_id="66666666-6666-4666-8666-666666666666",
            decision="APPROVED",
            actor_type="HUMAN",
            actor_id="reviewer@example.test",
            decided_at=_NOW,
        )
        return SimpleNamespace(
            run_id=_RUN_ID,
            pile_id=_PILE_ID,
            baseline_run_id="77777777-7777-4777-8777-777777777777",
            change_set_id="88888888-8888-4888-8888-888888888888",
            trigger_source=source,
            committed_at=_NOW,
            output_id=_OUTPUT_ID,
            output_content_hash="sha256:" + "e" * 64,
            commit_actor=SimpleNamespace(actor_type="SYSTEM", actor_id="task1-worker"),
            document_changes=(
                SimpleNamespace(
                    document_id=_DOCUMENT_ID,
                    kind="ADDED",
                    baseline_source=None,
                    target_source=source,
                ),
            ),
            items=(
                SimpleNamespace(
                    change_item_id="99999999-9999-4999-8999-999999999999",
                    kind="FACT_CREATED",
                    outcome="APPLIED",
                    fact=SimpleNamespace(
                        fact_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        origin_run_id=_RUN_ID,
                        subject="cash to close",
                        predicate="amount",
                        object_value={"currency": "USD", "value": 1250},
                        status="CONFIRMED",
                    ),
                    review=review,
                    evidence=(
                        SimpleNamespace(
                            evidence_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                            source=source,
                            chunk_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                            page_number=3,
                            start_offset=120,
                            end_offset=142,
                            source_text="Cash to close: $1,250",
                            content_hash="sha256:" + "f" * 64,
                        ),
                    ),
                ),
            ),
            whole_update_review=None,
        )


class _ReportQuery:
    def read_report_content(self, run_id: str):
        assert run_id == _RUN_ID
        return (
            SimpleNamespace(
                output_id=_OUTPUT_ID,
                run_id=_RUN_ID,
                output_type="FINAL_REPORT",
                storage_uri="artifact://sha256/" + "b" * 64,
                content_hash="sha256:" + "b" * 64,
                created_at=_NOW,
                render_input_fingerprint="sha256:" + "c" * 64,
                focused_update=None,
            ),
            b"# Final Report\n\nVerified.\n",
        )


def _call(server, name: str, arguments: dict[str, object]):
    return asyncio.run(server.call_tool(name, arguments))



def test_rest_and_mcp_share_the_same_production_query_service_factory() -> None:
    provider = default_mcp_service_provider()

    assert provider.query is get_external_query_service
    assert provider.change_history is get_change_history_service
    assert get_rest_external_query_service is get_external_query_service
    assert get_rest_change_history_service is get_change_history_service


def test_mcp_server_registers_exact_locked_tool_contract_without_resolving_services() -> None:
    server = create_mcp_server(_provider())

    tools = asyncio.run(server.list_tools())

    assert tuple(tool.name for tool in tools) == MCP_TOOL_NAMES
    assert "commit_run" not in {tool.name for tool in tools}
    assert all(tool.input_schema for tool in tools)
    assert all(tool.output_schema is not None for tool in tools)


def test_create_pile_tool_calls_application_service_and_returns_structured_output() -> None:
    service = _CreatePile()
    server = create_mcp_server(_provider(create_pile=lambda: service))

    result = _call(
        server,
        "create_pile",
        {
            "name": "Acme",
            "description": "Contracts",
            "watch_enabled": False,
            "watch_location": None,
        },
    )

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["pile_id"] == _PILE_ID
    assert service.command is not None
    assert service.command.name == "Acme"
    assert service.command.description == "Contracts"


def test_ingest_document_tool_decodes_exact_base64_bytes_and_calls_shared_ingestion_facade() -> None:
    service = _Ingestion()
    server = create_mcp_server(_provider(ingestion=lambda: service))
    source = b"contract text\nwith exact bytes\x00"

    result = _call(
        server,
        "ingest_document",
        {
            "pile_id": _PILE_ID,
            "logical_key": "master-contract",
            "filename": "contract.txt",
            "media_type": "text/plain",
            "source_base64": base64.b64encode(source).decode("ascii"),
        },
    )

    assert result.is_error is False
    assert service.source_bytes == source
    assert result.structured_content is not None
    assert result.structured_content["document_version_id"] == _VERSION_ID
    assert result.structured_content["created_run_ids"] == [_RUN_ID]


def test_ingest_document_rejects_malformed_base64_before_application_service() -> None:
    service = _Ingestion()
    server = create_mcp_server(_provider(ingestion=lambda: service))

    with pytest.raises(
        ToolError,
        match="source_base64 must contain valid canonical base64",
    ):
        _call(
            server,
            "ingest_document",
            {
                "pile_id": _PILE_ID,
                "logical_key": "master-contract",
                "filename": "contract.txt",
                "media_type": "text/plain",
                "source_base64": "%%%not-base64%%%",
            },
        )

    assert service.source_bytes == b""


def test_advance_run_tool_preserves_the_external_human_gate() -> None:
    server = create_mcp_server(_provider(advance=lambda: _AdvanceBlocked()))

    with pytest.raises(ToolError, match="human review"):
        _call(server, "advance_run", {"run_id": _RUN_ID})


def test_get_run_report_returns_verified_metadata_and_exact_markdown_content() -> None:
    server = create_mcp_server(_provider(review_query=lambda: _ReportQuery()))

    result = _call(server, "get_run_report", {"run_id": _RUN_ID})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["output_id"] == _OUTPUT_ID
    assert result.structured_content["content_markdown"] == "# Final Report\n\nVerified.\n"


def test_list_pile_change_history_tool_returns_machine_readable_commit_provenance() -> None:
    server = create_mcp_server(_provider(change_history=lambda: _HistoryQuery()))

    result = _call(
        server,
        "list_pile_change_history",
        {"pile_id": _PILE_ID, "limit": 7},
    )

    assert result.is_error is False
    assert result.structured_content is not None
    history = result.structured_content["result"][0]
    assert history["run_id"] == _RUN_ID
    assert history["trigger_source"]["logical_key"] == "closing-disclosure.pdf"
    assert history["items"][0]["outcome"] == "APPLIED"
    assert history["items"][0]["review"]["actor_id"] == "reviewer@example.test"
    assert history["items"][0]["evidence"][0]["source_text"] == "Cash to close: $1,250"


def test_get_run_change_history_tool_explains_one_committed_update() -> None:
    server = create_mcp_server(_provider(change_history=lambda: _HistoryQuery()))

    result = _call(server, "get_run_change_history", {"run_id": _RUN_ID})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["run_id"] == _RUN_ID
    assert result.structured_content["commit_actor"]["actor_id"] == "task1-worker"
