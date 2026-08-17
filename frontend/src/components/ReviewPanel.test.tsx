import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { api } from "../lib/api";
import type { RunReview } from "../lib/types";
import { ReviewPanel } from "./ReviewPanel";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      reviewFinding: vi.fn(),
      reviewUpdate: vi.fn()
    }
  };
});

const review: RunReview = {
  run_id: "22222222-2222-4222-8222-222222222222",
  run_type: "UPDATE",
  run_status: "REVIEW_REQUIRED",
  finding_review_complete: false,
  review_complete: false,
  commit_ready: false,
  findings: [
    {
      run_id: "22222222-2222-4222-8222-222222222222",
      finding_id: "33333333-3333-4333-8333-333333333333",
      relation: "CREATED",
      origin_run_id: "22222222-2222-4222-8222-222222222222",
      finding_type: "RULE_VIOLATION",
      rule_id: "44444444-4444-4444-8444-444444444444",
      title: "Payment term exceeds maximum",
      description: "Net 60 conflicts with the configured 45 day maximum.",
      severity: "HIGH",
      status: "PENDING_REVIEW",
      evidence: [
        {
          evidence_id: "55555555-5555-4555-8555-555555555555",
          document_version_id: "66666666-6666-4666-8666-666666666666",
          chunk_id: "77777777-7777-4777-8777-777777777777",
          page_number: 2,
          start_offset: 10,
          end_offset: 28,
          source_text: "Payment is due Net 60.",
          content_hash: `sha256:${"a".repeat(64)}`
        }
      ],
      decision: null
    }
  ],
  update_change_set: {
    change_set_id: null,
    durable: false,
    change_set_fingerprint: `sha256:${"b".repeat(64)}`,
    manifest: { added_fact_ids: ["fact-1"] },
    decision: null
  }
};

test("shows exact evidence and records an item-level finding decision", async () => {
  const onChanged = vi.fn();
  render(<ReviewPanel actorId="alice@example.test" onChanged={onChanged} review={review} />);

  expect(screen.getByText("Payment is due Net 60.")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Approve" }));

  await waitFor(() => expect(api.reviewFinding).toHaveBeenCalledWith(
    review.run_id,
    review.findings[0].finding_id,
    "APPROVED",
    "alice@example.test"
  ));
  expect(onChanged).toHaveBeenCalled();
});

test("does not enable update approval until finding review is complete", () => {
  render(<ReviewPanel actorId="alice@example.test" onChanged={() => undefined} review={review} />);
  expect(screen.getByRole("button", { name: "Approve update" })).toBeDisabled();
});
