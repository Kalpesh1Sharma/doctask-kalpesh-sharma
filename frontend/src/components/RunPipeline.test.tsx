import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import type { Run } from "../lib/types";
import { RunPipeline } from "./RunPipeline";

const run: Run = {
  run_id: "run",
  pile_id: "pile",
  run_type: "INITIAL",
  status: "RUNNING",
  trigger_document_version_id: null,
  parent_run_id: null,
  baseline_run_id: null,
  blocked_on_run_id: null,
  commit_required: true,
  started_at: null,
  completed_at: null,
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:00:00Z",
  steps: [
    {
      step_id: "ingest-1",
      stage: "INGEST",
      status: "COMPLETED",
      attempt: 1,
      started_at: null,
      completed_at: null,
      error_code: null,
      error_message: null,
      retry_operation_scope: null,
      retry_operation_key: null,
      retry_from_claim_generation: null,
      created_at: "2026-08-16T00:00:00Z"
    },
    {
      step_id: "examine-1",
      stage: "EXAMINE",
      status: "RETRYING",
      attempt: 1,
      started_at: null,
      completed_at: null,
      error_code: "RETRYABLE_PROVIDER_FAILURE",
      error_message: "retry",
      retry_operation_scope: null,
      retry_operation_key: null,
      retry_from_claim_generation: null,
      created_at: "2026-08-16T00:00:00Z"
    },
    {
      step_id: "examine-2",
      stage: "EXAMINE",
      status: "RUNNING",
      attempt: 2,
      started_at: null,
      completed_at: null,
      error_code: null,
      error_message: null,
      retry_operation_scope: null,
      retry_operation_key: null,
      retry_from_claim_generation: 1,
      created_at: "2026-08-16T00:00:01Z"
    }
  ]
};

test("renders all five durable stages and the latest attempt for each stage", () => {
  render(<RunPipeline run={run} />);

  for (const stage of ["INGEST", "UNDERSTAND", "EXAMINE", "REVIEW", "COMMIT"]) {
    expect(screen.getByText(stage)).toBeInTheDocument();
  }
  expect(screen.getByText("attempt 2")).toBeInTheDocument();
  expect(screen.queryByText("RETRYABLE_PROVIDER_FAILURE: retry")).not.toBeInTheDocument();
});
