"""MCP operation-name contract.

The MCP server is implemented in Phase 6B-4.  Keeping the names here prevents
REST and MCP from drifting into different product vocabularies meanwhile.
"""

from api.interface_contract import MCP_TOOL_BY_OPERATION, ExternalOperation

MCP_TOOL_NAMES: tuple[str, ...] = tuple(
    MCP_TOOL_BY_OPERATION[operation] for operation in ExternalOperation
)
