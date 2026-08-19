import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import { ApiError, api } from "../lib/api";
import type { FinalReport, Run, RunStep } from "../lib/types";
import { RunWorkspace } from "./RunWorkspace";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getRun: vi.fn(),
      advanceRun: vi.fn(),
      getFinalReport: vi.fn(),
      getFinalReportContent: vi.fn()
    }
  };
});

const RUN_ID = "3728d00d-1fc0-4d8a-8dca-b1ee4aff96cc";
const NOW = "2026-08-18T13:34:52.551118Z";

function step(stage: RunStep["stage"], status: string): RunStep {
  return {
    step_id: `${stage.toLowerCase()}-step`,
    stage,
    status,
    attempt: 1,
    started_at: NOW,
    completed_at: status === "COMPLETED" ? NOW : null,
    error_code: null,
    error_message: null,
    retry_operation_scope: null,
    retry_operation_key: null,
    retry_from_claim_generation: null,
    created_at: NOW
  };
}

const reviewRequiredRun: Run = {
  run_id: RUN_ID,
  pile_id: "5fa6ace3-2bf7-4732-a18f-044640bb3281",
  run_type: "INITIAL",
  status: "REVIEW_REQUIRED",
  trigger_document_version_id: null,
  parent_run_id: null,
  baseline_run_id: null,
  blocked_on_run_id: null,
  commit_required: true,
  started_at: NOW,
  completed_at: null,
  created_at: NOW,
  updated_at: NOW,
  steps: [
    step("INGEST", "COMPLETED"),
    step("UNDERSTAND", "COMPLETED"),
    step("EXAMINE", "COMPLETED"),
    step("REVIEW", "COMPLETED"),
    step("COMMIT", "PENDING")
  ]
};

const completedRun: Run = {
  ...reviewRequiredRun,
  status: "COMPLETED",
  completed_at: NOW,
  steps: reviewRequiredRun.steps.map((item) => item.stage === "COMMIT" ? step("COMMIT", "COMPLETED") : item)
};

const finalReport: FinalReport = {
  output_id: "892630d1-070c-48b1-b34e-f72dbec8a4b0",
  run_id: RUN_ID,
  output_type: "FINAL_REPORT",
  storage_uri: "artifact://sha256/test",
  content_hash: `sha256:${"d".repeat(64)}`,
  created_at: NOW,
  render_input_fingerprint: `sha256:${"e".repeat(64)}`,
  focused_update: null
};

beforeEach(() => {
  vi.clearAllMocks();
});

test("reconciles a stale manual advance when the worker already completed COMMIT", async () => {
  vi.mocked(api.getRun)
    .mockResolvedValueOnce(reviewRequiredRun)
    .mockResolvedValueOnce(reviewRequiredRun)
    .mockResolvedValueOnce(completedRun);
  vi.mocked(api.advanceRun).mockRejectedValue(
    new ApiError(500, "RUN_WORKFLOW_INTEGRITY", "durable state failed an integrity check")
  );
  vi.mocked(api.getFinalReport).mockResolvedValue(finalReport);
  vi.mocked(api.getFinalReportContent).mockResolvedValue("# Final Report\n\nCommitted by worker.\n");

  render(<RunWorkspace actorId="reviewer@example.test" onRunRefreshed={() => undefined} ruleSets={[]} runId={RUN_ID} />);

  const advanceButton = await screen.findByRole("button", { name: "Advance after review" });
  fireEvent.click(advanceButton);

  await waitFor(() => expect(api.advanceRun).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByRole("button", { name: "Run completed" })).toBeDisabled());
  expect(screen.queryByText(/RUN_WORKFLOW_INTEGRITY/)).not.toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Final report" })).toBeInTheDocument();
  expect(screen.getByText(/Committed by worker/)).toBeInTheDocument();
});
