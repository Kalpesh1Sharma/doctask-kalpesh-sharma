# Phase 7B Task 1 Demo Runbook

This runbook is for **Task 1 only** and follows the final live-verified sequence.

## 0. Before recording

Use synthetic data only. Keep `.env`, API keys, and any terminal containing secrets out of the recording.

From the repository root, start the operational stack:

```powershell
docker compose up -d --build
docker compose ps
```

Open:

```text
UI:  http://127.0.0.1:5173
API: http://127.0.0.1:8000
```

Create a clean host watch directory:

```powershell
Remove-Item .\var\watch\task1-demo-001 -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force .\var\watch\task1-demo-001 | Out-Null
```

## 1. Create the Task 1 demo pile

Use the checked-in setup script:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\setup_task1_demo_pile.ps1
```

Save the printed `pile_id`. The script creates the watched pile and the small user-supplied rule set from `rules/task1_demo_rules.json`.

The rules are intentionally small. They prove the Task 1 “user hands it rules” behavior; they do not recreate the separate Task 2 product.

## 2. Initial run: Closing Disclosure only

Copy **only** the Closing Disclosure first:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\copy_baseline.ps1
```

Do not copy the Loan Estimate or Underwriting Approval yet. The watcher treats each arrival as an incremental event, so copying several files at once would intentionally create multiple Runs.

Watch the UI and/or logs:

```powershell
docker compose logs --since=3m watcher
docker compose logs --since=3m worker
```

The expected visible path is:

```text
INGEST -> UNDERSTAND -> EXAMINE -> REVIEW -> COMMIT
```

The initial Run should stop at `REVIEW_REQUIRED` with the grounded rule finding:

```text
Cash to Close: $19,250
Rule limit:    $19,000
Result:        violation
```

## 3. Review and commit the INITIAL run

In the React review UI:

1. Open the INITIAL Run at `REVIEW_REQUIRED`.
2. Show the `$19,250` rule finding and exact quoted source evidence.
3. Approve that legitimate finding.
4. Do **not** immediately press `Advance after review`; allow the worker to reconcile and enter COMMIT normally.
5. Refresh after a few seconds and confirm the Run becomes `COMPLETED`.
6. Open the verified final report.

This proves the human gate, durable review decision, automatic continuation, and committed grounded artifact.

## 4. Focused UPDATE: add the Loan Estimate

After the INITIAL Run is `COMPLETED`, copy the Loan Estimate as a new watched document:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\copy_loan_estimate.ps1
```

The watcher should create one UPDATE Run linked to the completed INITIAL baseline.

The expected source-backed disagreements are:

```text
Interest Rate
Closing Disclosure: 6.375%
Loan Estimate:       6.250%

Cash to Close
Closing Disclosure: $19,250
Loan Estimate:       $18,750
```

The UPDATE should reach `REVIEW_REQUIRED` with both contradictions visible. The `$18,750` Loan Estimate value must **not** create a new `cash_to_close_limit` violation because it is below the $19,000 threshold.

## 5. Review contradiction findings

Approve both contradiction findings because both are genuine source disagreements:

```text
Interest Rate contradiction   -> APPROVE
Cash to Close contradiction   -> APPROVE
```

These approvals acknowledge that the sources disagree; they do not declare either source universally correct.

## 6. Mixed UPDATE fact-change review

The UPDATE then presents item-by-item proposed fact changes. Use the visible fact content, not hard-coded runtime UUIDs.

For the verified demo corpus, use this mixed decision set:

```text
Interest Rate = 6.250%                -> APPROVE CHANGE
Estimated Cash to Close = $18,750     -> APPROVE CHANGE
Borrower = Jordan Lee                 -> REJECT CHANGE
Property Address = same address       -> REJECT CHANGE
Loan Amount = $320,000                -> REJECT CHANGE
Loan Term = 30 years                  -> REJECT CHANGE
Escrow Account = Yes                  -> REJECT CHANGE
File Reference = TASK1-DEMO-001       -> REJECT CHANGE
```

This is the strongest item-level review proof: two genuine changes are applied while six duplicate restatements are rejected.

After the last required decision, allow the worker to continue normally. Refresh after a few seconds and confirm:

```text
UPDATE = COMPLETED
COMMIT = COMPLETED
Focused update proof = Present
```

Open the Report tab and show the verified committed artifact and focused-update proof.

## 7. Show change history

Use the UI/REST/MCP surface to answer:

```text
What changed?
When did it commit?
Which source/version caused it?
What exact evidence supports the proposal?
What did the human approve or reject?
Which output/Run committed it?
```

REST:

```text
GET /api/v1/piles/{pile_id}/change-history
GET /api/v1/runs/{run_id}/change-history
```

MCP:

```text
list_pile_change_history
get_run_change_history
```

## 8. Optional hostile-source proof

Do this only after the clean two-document demo is complete, and only if time permits.

Replace the watched Closing Disclosure with the hostile synthetic revision:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\copy_update.ps1
```

The hostile text is document content, not workflow authority. The success condition is that it cannot auto-approve findings, skip human review, reveal environment variables, or directly mark a Run completed.

## 9. Second independent corpus proof

After the primary demo is complete, keep that successful pile intact and create the independent Priya Raman pile:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\setup_second_corpus.ps1
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\copy_second_baseline.ps1
```

The second Closing Disclosure is below the configured cash-to-close threshold and should produce an honest zero-Finding result. When the INITIAL reaches `REVIEW_REQUIRED`, confirm the empty Finding list and click **Acknowledge zero findings**. This explicit human action is required before COMMIT; zero Findings are never treated as implicit approval. After the worker completes the Run, verify it with the Run id shown in the UI/API:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\verify_no_findings.ps1 -RunId <RUN_ID>
```

After that INITIAL Run is complete, add the agreeing Loan Estimate as a focused UPDATE:

```powershell
powershell -ExecutionPolicy Bypass -File .\demo\task1\scripts\copy_second_loan_estimate.ps1
```

It should introduce no contradiction/rule Finding for the agreeing values. Review any proposed Fact items normally using the baseline/proposed/source details shown on each review card.

## 10. Regression proof

Do not spend demo time running the full suite. The repository contains automated verification for restart/concurrency/hostile-document and other edge behaviors. Before submission, keep the normal quality gates green:

```powershell
pytest -q
ruff check .
python -m pip check
git diff --check
```

The repository also includes the Phase 6C operational/final verification scripts for broader proof.
