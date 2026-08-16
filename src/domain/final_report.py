"""Frozen, canonical input for deterministic final Markdown rendering."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from domain.render_input import RenderInputManifest
from domain.update_review import UpdateChangeSetManifest

FINAL_REPORT_INPUT_FORMAT = "final-report-input-v1"
MARKDOWN_RENDERER_VERSION = "markdown-v1"


class FinalReportInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["final-report-input-v1"] = FINAL_REPORT_INPUT_FORMAT
    renderer_version: Literal["markdown-v1"] = MARKDOWN_RENDERER_VERSION
    render_input: RenderInputManifest
    update_change_set: UpdateChangeSetManifest | None = None

    @model_validator(mode="after")
    def validate_update_binding(self) -> FinalReportInput:
        if (
            self.update_change_set is not None
            and self.update_change_set.run_id != self.render_input.run_id
        ):
            raise ValueError("UPDATE change set and render input must identify the same Run")
        return self

    @property
    def run_id(self) -> str:
        return str(UUID(self.render_input.run_id))


def final_report_input_canonical_bytes(value: FinalReportInput) -> bytes:
    payload = {
        "format": value.format,
        "renderer_version": value.renderer_version,
        "render_input": value.render_input.model_dump(mode="json"),
        "update_change_set": (
            value.update_change_set.model_dump(mode="json")
            if value.update_change_set is not None
            else None
        ),
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def final_report_input_fingerprint(value: FinalReportInput) -> str:
    return f"sha256:{sha256(final_report_input_canonical_bytes(value)).hexdigest()}"
