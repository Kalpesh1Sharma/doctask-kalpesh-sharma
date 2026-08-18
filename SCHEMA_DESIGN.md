# Schema Design — Final 001–008 Contract

## 1. Purpose

This document summarizes the final relational model after Phase 6C. The executable SQL migrations are authoritative.

Current ordered domain migration chain:

```text
001_schema_v2_7.sql
002_run_rule_scope.sql
003_update_review.sql
004_idempotency_claim_generation.sql
005_run_step_retry_history.sql
006_document_runtime_commit_and_telemetry.sql
007_watch_folder_document_classification.sql
008_item_level_update_review.sql
```

Migration 008 is the latest domain migration. The public application schema contains exactly **31 base tables**.

Deployment migration receipts live separately in `task1_meta.schema_migrations`; that table is not part of the public/domain table count.

## 2. Final public tables

```text
 1. piles
 2. documents
 3. document_versions
 4. document_chunks
 5. evidence_spans
 6. rule_sets
 7. rules
 8. runs
 9. run_document_versions
10. run_steps
11. facts
12. run_facts
13. fact_evidence
14. findings
15. run_findings
16. finding_evidence
17. review_decisions
18. outputs
19. idempotency_records
20. audit_events
21. run_metrics
22. run_rule_scopes
23. run_rule_set_snapshots
24. run_rule_snapshots
25. update_change_sets
26. update_review_decisions
27. provider_usage_events
28. document_classifications
29. document_classification_evidence
30. update_change_items
31. update_item_review_decisions
```

## 3. Core durable model

```text
PILE
 ├── DOCUMENT
 │    └── DOCUMENT_VERSION
 │         ├── DOCUMENT_CHUNK (pgvector embedding)
 │         ├── EVIDENCE_SPAN
 │         └── DOCUMENT_CLASSIFICATION
 │              └── DOCUMENT_CLASSIFICATION_EVIDENCE
 │
 ├── RULE_SET
 │    └── RULE
 │
 └── RUN
      ├── RUN_DOCUMENT_VERSION
      ├── RUN_STEP
      ├── captured rule scope/snapshots
      ├── RUN_FACT -> FACT -> FACT_EVIDENCE -> EVIDENCE_SPAN
      ├── RUN_FINDING -> FINDING -> FINDING_EVIDENCE -> EVIDENCE_SPAN
      ├── REVIEW_DECISION
      ├── UPDATE_CHANGE_SET
      │    ├── UPDATE_CHANGE_ITEM
      │    ├── UPDATE_ITEM_REVIEW_DECISION
      │    └── UPDATE_REVIEW_DECISION (zero-item/legacy whole-set path)
      ├── OUTPUT
      ├── IDEMPOTENCY_RECORD
      ├── PROVIDER_USAGE_EVENT
      ├── AUDIT_EVENT
      └── RUN_METRIC
```

## 4. Durable workflow state

Overall `run_status` and per-attempt `run_step_status` remain separate.

```text
run_status:
  PENDING
  RUNNING
  REVIEW_REQUIRED
  COMPLETED
  FAILED

run_stage:
  INGEST
  UNDERSTAND
  EXAMINE
  REVIEW
  COMMIT

run_step_status:
  PENDING
  RUNNING
  AWAITING_UPSTREAM
  COMPLETED
  RETRYING
  SKIPPED
  ESCALATED
  FAILED
```

Each retry is a distinct `(run_id, stage, attempt)` row and has a matching per-attempt metric when closed.

Migration 006 introduced the durable COMMIT/finalization cutover so fresh Phase-6 runs cannot complete through an old direct-finalization path.

## 5. Document/version integrity

`documents` is the logical identity; `document_versions` is immutable version history.

Key invariants include:

- unique `(pile_id, logical_key)`;
- unique `(document_id, version_number)`;
- unique `(document_id, content_hash)`;
- current version belongs to the same Document;
- current version must be READY;
- current version only moves forward;
- READY/FAILED terminal version state is not silently rewritten;
- content-addressed source integrity is also verified by the application storage layer.

Concurrency-sensitive version creation locks the parent Document row so different-content versions receive unique contiguous version numbers.

## 6. Evidence and provenance

An EvidenceSpan points to a specific DocumentVersion and DocumentChunk. The composite database relationship prevents an EvidenceSpan from claiming one version while pointing at a chunk from another.

Facts and Findings are historical entities. Run membership is represented separately through `run_facts` and `run_findings`, allowing `CREATED` versus `CARRIED_FORWARD` state without rewriting origin history.

Claim-to-evidence relationships are explicit through `fact_evidence` and `finding_evidence`.

## 7. Rule-scope snapshots

Migration 002 adds durable run-scope capture:

- `run_rule_scopes`;
- `run_rule_set_snapshots`;
- `run_rule_snapshots`.

A run therefore evaluates the rule definition that was captured for that run rather than silently changing when a mutable external configuration changes later.

## 8. UPDATE review model

Migration 003 introduced fingerprint-bound UPDATE change-set review.

Migration 008 makes material UPDATE review item-level:

- `update_change_items` records the material Fact-level proposal represented by a change set;
- `update_item_review_decisions` records the explicit human APPROVED/REJECTED decision for each item.

Mixed decisions are allowed in one review. Rejected proposals remain historical/auditable but are excluded from effective committed state and the focused final artifact.

The zero-material-item path can use the whole-change-set decision row because there is no item to review.

## 9. Document-role classification

Migration 007 adds the controlled `document_role` classification model:

```text
LOAN_ESTIMATE
CLOSING_DISCLOSURE
PROMISSORY_NOTE
SETTLEMENT_STATEMENT
UNDERWRITING_APPROVAL
UNKNOWN
```

`document_classifications` is one immutable classification per READY DocumentVersion.

Known roles require evidence rows in `document_classification_evidence`, and each evidence chunk must belong to the classified DocumentVersion. `UNKNOWN` is the conservative fallback.

## 10. Vector retrieval

`document_chunks.embedding` is stored in pgvector and indexed with HNSW cosine operations.

The original V2.7 vector width remains the executable schema contract. Application retrieval uses matching document/query embeddings and bounded top-k search.

Vector similarity selects candidate context; exact evidence rows remain the provenance authority.

## 11. Idempotency and fencing

`idempotency_records` protects expensive logical operations through a unique logical identity plus claim generation.

The provider call is not held inside a long database transaction. The pattern is:

```text
claim durable operation
        ↓
perform provider/expensive work
        ↓
transactionally persist result + authoritative domain state
        ↓
mark exact generation completed
```

A stale owner cannot finalize a newer generation's work.

## 12. COMMIT/output authority

Fresh final report completion is owned by the COMMIT workflow path.

The database/application boundary requires the current authoritative render input, human UPDATE approval where applicable, deterministic artifact identity, active/current COMMIT attempt, and idempotency generation to agree before the run becomes COMPLETED.

Already-completed historical pre-cutover outputs may be replayed read-only; they do not create a second fresh finalization path.

## 13. Provider telemetry and metrics

`provider_usage_events` is generation/attempt scoped and records provider/model identity plus usage when available.

State semantics distinguish started, recorded, and unavailable receipts. Unknown usage/cost remains NULL rather than being fabricated.

`run_metrics` stores stage/attempt duration and aggregates known provider usage/cost for every closed attempt, including retry/escalation/failure paths.

## 14. Historical reconstruction

The schema can reconstruct:

```text
What source versions were in the run?
Which Facts/Findings existed and which were carried forward?
What exact EvidenceSpans supported them?
Which rules were frozen for the run?
What did the human approve/reject?
What UPDATE items were proposed and actually applied?
Which source triggered the UPDATE?
What output was committed?
When did it happen?
How many attempts were required and what did they cost?
```

Change history is derived from these immutable authoritative rows; no duplicate change-history table is required.

## 15. Concurrency model

Authoritative protections include:

- parent-row locking for document-version creation/current-version advancement;
- unique UPDATE trigger claims;
- Run row locking for review/finalization decisions;
- idempotency unique identities and generations;
- run-step attempt uniqueness;
- database transition/immutability triggers;
- restrictive historical foreign keys.

The background worker's PostgreSQL advisory lock is transient scheduling coordination and is not represented as a new durable schema authority.

## 16. Migration policy

Migrations are append-only historical contracts.

- Do not rewrite migrations 001–008 after closure.
- Add a new numbered migration for any future schema change.
- `docker/migrate.sh` may store deployment receipts/checksums outside `public`, but those receipts do not alter the domain migration sequence.
- The executable SQL and clean ordered migration tests win over prose if a documentation mismatch is discovered.
