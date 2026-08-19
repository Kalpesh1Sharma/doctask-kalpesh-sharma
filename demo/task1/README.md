# Phase 7B — Task 1 Demo Pack

This pack is **only for Task 1: the agentic document-pile system**.

It uses the mortgage/loan-file domain because the finished Task 1 implementation supports mortgage document roles. It is **not** the separate Task 2 assigned build, “Closing document reconciliation memo.”

## What this pack demonstrates

The primary live-verified sequence is intentionally incremental:

1. Closing Disclosure arrives -> INITIAL Run -> human review -> COMMIT.
2. Loan Estimate arrives -> focused UPDATE -> source contradictions -> item-by-item mixed review -> COMMIT.

That sequence demonstrates:

- watched-folder arrival;
- semantic document classification;
- grounded fact extraction with exact source evidence;
- disagreement/contradiction detection;
- user-supplied rule evaluation;
- visible `INGEST -> UNDERSTAND -> EXAMINE -> REVIEW -> COMMIT` execution;
- explicit human approve/reject decisions;
- focused UPDATE behavior when a new related document arrives;
- mixed UPDATE item decisions;
- durable change history answering what changed, when, and because of which source;
- a verified committed report with focused-update proof.

The pack also includes hostile-source material plus a second independent corpus for the required post-demo “works again on different documents” / honest no-findings proof.

## Explicit non-goals

This pack does **not** demonstrate Task 2-specific requirements such as:

- a “closing document reconciliation memo” product;
- side-by-side variance classification as tolerable/curable;
- correction-letter drafting;
- revised-disclosure-request drafting.

Those belong to Task 2 and should be demonstrated separately.

## Pack contents

```text
baseline/       Synthetic Task 1 mortgage sources, introduced sequentially in the main demo
update/         Optional hostile replacement version of the same Closing Disclosure
second_corpus/  Different borrower/property/loan values for the second-pile no-findings proof
rules/          Small Task 1 user-rule payload
scripts/        Convenience PowerShell scripts for the Docker-operated demo
DEMO_RUNBOOK.md Final live-verified rehearsal sequence
EXPECTED_BEHAVIOR.md Durable acceptance behavior without assuming exact LLM wording/counts
```
