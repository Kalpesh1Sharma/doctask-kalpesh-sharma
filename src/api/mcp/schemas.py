"""MCP-specific structured input/output DTOs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue

from api.rest.schemas.review import FinalReportResponse


class McpModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class McpRuleInput(McpModel):
    rule_key: str
    title: str
    description: str | None = None
    severity: str
    definition: JsonValue


class McpFinalReportResult(FinalReportResponse):
    """Verified final report metadata plus the exact committed Markdown text."""

    content_markdown: str
