from api.interface_contract import MCP_TOOL_BY_OPERATION, REST_OPERATIONS, ExternalOperation
from api.mcp.contracts import MCP_TOOL_NAMES


def test_every_external_operation_has_rest_and_mcp_identity() -> None:
    rest_operations = {item.operation for item in REST_OPERATIONS}

    assert rest_operations == set(ExternalOperation)
    assert set(MCP_TOOL_BY_OPERATION) == set(ExternalOperation)


def test_rest_method_and_path_pairs_are_unique_and_versioned() -> None:
    identities = {(item.method, item.path) for item in REST_OPERATIONS}

    assert len(identities) == len(REST_OPERATIONS)
    assert all(item.path.startswith("/api/v1/") for item in REST_OPERATIONS)
    assert all(item.method in {"GET", "POST"} for item in REST_OPERATIONS)


def test_mcp_tool_names_are_unique_and_stable() -> None:
    assert len(MCP_TOOL_NAMES) == len(set(MCP_TOOL_NAMES))
    assert MCP_TOOL_NAMES == tuple(operation.value for operation in ExternalOperation)


def test_mcp_contract_contains_no_direct_commit_bypass_tool() -> None:
    assert "commit_run" not in MCP_TOOL_NAMES
