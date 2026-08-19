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
      acknowledgeRunReview: vi.fn(),
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
  run_review_acknowledgement: null,
  update_change_set: {
    change_set_id: null,
    durable: false,
    change_set_fingerprint: `sha256:${"b".repeat(64)}`,
    manifest: { added_fact_ids: ["fact-1"] },
    items: [],
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


test("records UPDATE Fact decisions independently", async () => {
  const onChanged = vi.fn();
  const itemized: RunReview = {
    ...review,
    findings: [],
    finding_review_complete: true,
    update_change_set: {
      ...review.update_change_set!,
      durable: true,
      change_set_id: "88888888-8888-4888-8888-888888888888",
      items: [
        {
          item_id: "99999999-9999-4999-8999-999999999999",
          fact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          kind: "FACT_CREATED",
          payload: { fact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", kind: "CREATED" },
          fact: {
            fact_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            subject: "Loan Estimate",
            predicate: "Interest Rate",
            object_value: "6.250%",
            display_value: "6.250%",
            status: "CONFIRMED",
            concept: "interest rate",
            evidence: [
              {
                evidence_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                document_version_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                logical_key: "loan_estimate",
                chunk_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
                page_number: null,
                start_offset: 10,
                end_offset: 31,
                source_text: "Interest Rate: 6.250%",
                content_hash: `sha256:${"c".repeat(64)}`
              }
            ]
          },
          baseline_facts: [
            {
              fact_id: "12121212-1212-4212-8212-121212121212",
              subject: "Interest Rate",
              predicate: "is",
              object_value: "6.375%",
              display_value: "6.375%",
              status: "CONFIRMED",
              concept: "interest rate",
              evidence: [
                {
                  evidence_id: "13131313-1313-4313-8313-131313131313",
                  document_version_id: "14141414-1414-4414-8414-141414141414",
                  logical_key: "closing_disclosure",
                  chunk_id: "15151515-1515-4515-8515-151515151515",
                  page_number: null,
                  start_offset: 10,
                  end_offset: 31,
                  source_text: "Interest Rate: 6.375%",
                  content_hash: `sha256:${"d".repeat(64)}`
                }
              ]
            }
          ],
          decision: null
        },
        {
          item_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          fact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
          kind: "FACT_REMOVED",
          payload: { fact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc", kind: "REMOVED_FROM_CURRENT_STATE" },
          fact: {
            fact_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            subject: "Escrow Account",
            predicate: "is",
            object_value: "Yes",
            display_value: "Yes",
            status: "CONFIRMED",
            concept: "escrow account",
            evidence: []
          },
          baseline_facts: [],
          decision: null
        }
      ]
    }
  };

  render(<ReviewPanel actorId="alice@example.test" onChanged={onChanged} review={itemized} />);
  expect(screen.getByText("Interest Rate")).toBeInTheDocument();
  expect(screen.getByText("6.375%")).toBeInTheDocument();
  expect(screen.getByText("6.250%")).toBeInTheDocument();
  expect(screen.getByText("closing_disclosure")).toBeInTheDocument();
  expect(screen.getByText("Interest Rate: 6.375%")).toBeInTheDocument();
  expect(screen.getByText("loan_estimate")).toBeInTheDocument();
  expect(screen.getByText("Interest Rate: 6.250%")).toBeInTheDocument();
  const approveButtons = screen.getAllByRole("button", { name: "Approve change" });
  fireEvent.click(approveButtons[0]);

  await waitFor(() => expect(api.reviewUpdate).toHaveBeenCalledWith(
    itemized.run_id,
    "APPROVED",
    "alice@example.test",
    itemized.update_change_set!.change_set_fingerprint,
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
  ));
  expect(onChanged).toHaveBeenCalled();
});


test("requires explicit acknowledgement for a zero-finding INITIAL review", async () => {
  const onChanged = vi.fn();
  const cleanInitial: RunReview = {
    ...review,
    run_type: "INITIAL",
    findings: [],
    finding_review_complete: true,
    update_change_set: null,
    run_review_acknowledgement: null,
    review_complete: false,
    commit_ready: false
  };

  render(<ReviewPanel actorId="alice@example.test" onChanged={onChanged} review={cleanInitial} />);
  fireEvent.click(screen.getByRole("button", { name: "Acknowledge zero findings" }));

  await waitFor(() => expect(api.acknowledgeRunReview).toHaveBeenCalledWith(
    cleanInitial.run_id,
    "alice@example.test"
  ));
  expect(onChanged).toHaveBeenCalled();
});
