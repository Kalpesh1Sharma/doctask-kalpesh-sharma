# Schema Design — V2.7 + Reviewed Additive Migrations 002 and 003

## 1. Purpose

This document records the locked relational schema design for the project.

It is a **design-level schema reference**, not a replacement for the executable migration.

The executable database definition is now the ordered migration set:

```text
sql/migrations/001_schema_v2_7.sql
sql/migrations/002_run_rule_scope.sql
sql/migrations/003_update_review.sql
sql/migrations/004_idempotency_claim_generation.sql
sql/migrations/005_run_step_retry_history.sql
sql/migrations/004_idempotency_claim_generation.sql
sql/migrations/005_run_step_retry_history.sql
```

`001_schema_v2_7.sql` remains the locked historical V2.7 baseline and must not be rewritten.

`002_run_rule_scope.sql` is a reviewed additive correction for a verified provenance gap: V2.7 could not durably reconstruct the exact RuleSet/Rule scope evaluated by a historical Run, especially when that Run produced zero Rule-Violation Findings.

`003_update_review.sql` is an implemented/reviewed additive correction for UPDATE-level human approval, pending independent checkpoint approval: V2.7 could not durably bind a completed baseline, trigger source, prepared delta, and exact reviewed UPDATE state to one approval or rejection.

The schema design was evolved through multiple review rounds. The final model preserves:

- durable run history
- complete run-input provenance
- immutable facts/findings
- explicit carry-forward membership
- exact evidence-to-document-version traceability
- idempotent workflow operations
- human review history
- output history
- auditability
- per-stage retry/metrics history
- PostgreSQL-enforced concurrency and integrity constraints

---

# 2. Source-of-Truth Order

For schema questions, use this order:

1. Engineer/specification PDF — requirements.
2. Approved architecture/design documents — architectural decisions.
3. Executable migrations in order — exact database implementation:
   - approved locked baseline `001_schema_v2_7.sql`
   - approved additive `002_run_rule_scope.sql`
   - implemented/reviewed `003_update_review.sql`, pending independent checkpoint approval
4. This schema design — conceptual/data-model reference.
5. Application code — current implementation.

If this document and the executable migrations appear to disagree, do not silently change either one. Identify the discrepancy and verify which artifact is authoritative.

Migration `001` remains historically locked. Future schema changes must be additive, explicit, and reviewed rather than folded backward into `001`.

---

# 3. High-Level Entity Model

```text
PILE
 │
 ├── DOCUMENT
 │     │
 │     └── DOCUMENT_VERSION
 │             │
 │             ├── DOCUMENT_CHUNK
 │             │      └── embedding
 │             │
 │             └── EVIDENCE_SPAN
 │
 ├── RULE_SET
 │     └── RULE
 │
 └── RUN
       │
       ├── RUN_DOCUMENT_VERSION
       ├── RUN_RULE_SCOPE
       │     ├── RUN_RULE_SET_SNAPSHOT
       │     └── RUN_RULE_SNAPSHOT
       │
       ├── RUN_STEP
       │
       ├── FACT
       │     └── FACT_EVIDENCE
       │
       ├── RUN_FACT
       │
       ├── FINDING
       │     └── FINDING_EVIDENCE
       │
       ├── RUN_FINDING
       │     └── REVIEW_DECISION
       ├── UPDATE_CHANGE_SET
       │     └── UPDATE_REVIEW_DECISION
       │
       ├── OUTPUT
       ├── IDEMPOTENCY_RECORD
       ├── AUDIT_EVENT
       └── RUN_METRIC
```

The locked V2.7 baseline contains **21 tables**.

Reviewed migration `002_run_rule_scope.sql` adds **3 Run-owned rule-scope tables**, reviewed migration `003_update_review.sql` adds **2 UPDATE-review tables**, migrations `004` and `005` strengthen existing tables without adding new ones, and migration `006_document_runtime_commit_and_telemetry.sql` adds **1 provider-usage table**. The current executable schema after ordered migrations `001` through `006` therefore contains **27 tables**:

```text
1.  piles
2.  documents
3.  document_versions
4.  document_chunks
5.  evidence_spans
6.  rule_sets
7.  rules
8.  runs
9.  run_document_versions
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
```

---

# 4. PostgreSQL Types

The locked schema defines these PostgreSQL enums.

## `document_version_status`

```text
PENDING
READY
FAILED
```

## `run_type`

```text
INITIAL
UPDATE
MANUAL
```

## `run_status`

This is the status of the **overall Run**:

```text
PENDING
RUNNING
REVIEW_REQUIRED
COMPLETED
FAILED
```

`AWAITING_UPSTREAM` and `RETRYING` do NOT belong here.

---

## `run_stage`

```text
INGEST
UNDERSTAND
EXAMINE
REVIEW
COMMIT
```

---

## `run_step_status`

This describes an individual workflow stage/attempt:

```text
PENDING
RUNNING
AWAITING_UPSTREAM
COMPLETED
RETRYING
SKIPPED
ESCALATED
FAILED
```

Run status and run-step status are intentionally separate concepts.

---

## `fact_status`

```text
CONFIRMED
DISPUTED
SUPERSEDED
```

Fact rows are treated as immutable historical records.

---

## `run_membership_relation`

```text
CREATED
CARRIED_FORWARD
```

This records whether a fact/finding was created in the current run or carried forward from an earlier run.

---

## `finding_type`

```text
RULE_VIOLATION
CONTRADICTION
```

---

## `finding_status`

```text
PENDING
APPROVED
REJECTED
```

---

## `review_decision_type`

```text
APPROVED
REJECTED
```

---

## `idempotency_status`

```text
IN_PROGRESS
COMPLETED
FAILED
```

---

# 5. `piles`

A pile is the top-level collection/scope containing documents and their run history.

```text
piles
-----
id
name
description
watch_enabled
watch_location
latest_run_id
created_at
updated_at
```

Important properties:

- `id` is the primary key.
- `watch_enabled` controls watcher behavior.
- `latest_run_id` points to the latest run.
- `latest_run_id` is implemented as a foreign key after the `runs` table exists.
- Historical references use `ON DELETE RESTRICT`.

---

# 6. `documents`

A document represents a logical document identity inside a pile.

```text
documents
---------
id
pile_id
logical_key
current_version_id
created_at
updated_at
```

Constraint:

```text
UNIQUE(pile_id, logical_key)
```

`current_version_id` references a version of the same document.

The current version must:

- belong to the same document
- be `READY`
- move forward monotonically

The database protects this through the current-version validation trigger.

---

# 7. `document_versions`

A document version represents an immutable versioned representation of a logical document.

```text
document_versions
-----------------
id
document_id
version_number
content_hash
storage_uri
mime_type
size_bytes
processing_status
created_at
```

Constraints:

```text
UNIQUE(document_id, version_number)
UNIQUE(document_id, content_hash)
```

Processing status:

```text
PENDING
READY
FAILED
```

Important rule:

```text
current_version_id
    ↓
must reference a READY version
```

The current-version update must also be monotonic.

Document version creation/current-version advancement is serialized by locking the parent `documents` row with:

```sql
SELECT ... FOR UPDATE
```

The executable V2.7 DDL enforces the READY/current-version relationship with a trigger.

---

# 8. `document_chunks`

A document version is divided into searchable chunks.

```text
document_chunks
---------------
id
document_version_id
chunk_index
content
content_hash
page_number
start_offset
end_offset
embedding
created_at
```

Constraints:

```text
UNIQUE(document_version_id, chunk_index)
UNIQUE(id, document_version_id)
```

The second uniqueness constraint exists specifically to support the composite evidence foreign key.

Offset validation prevents an end offset from preceding a start offset.

The embedding is stored using pgvector.

The locked embedding configuration is:

```text
vector(1536)
```

with cosine similarity/retrieval and an HNSW ANN index using:

```text
vector_cosine_ops
```

The exact embedding dimension must remain aligned with the selected embedding model.

---

# 9. `evidence_spans`

An evidence span identifies an exact source location used to support a fact or finding.

```text
evidence_spans
--------------
id
document_version_id
chunk_id
page_number
start_offset
end_offset
quoted_text
content_hash
created_at
```

Critical integrity rule:

```text
evidence_spans.document_version_id
        MUST MATCH
document_chunks.document_version_id
```

This is enforced through a composite foreign key:

```sql
FOREIGN KEY (
    chunk_id,
    document_version_id
)
REFERENCES document_chunks (
    id,
    document_version_id
)
```

This prevents an evidence span from claiming one document version while pointing to a chunk belonging to another version.

Offsets are validated so an end offset cannot precede a start offset.

Evidence references use `ON DELETE RESTRICT`.

---

# 10. `rule_sets`

Rule sets group the rules applicable to a pile.

```text
rule_sets
---------
id
pile_id
name
description
version
created_at
```

`version` must be positive.

---

# 11. `rules`

A rule represents a concrete validation/checking rule.

```text
rules
-----
id
rule_set_id
rule_key
title
description
severity
definition
created_at
```

Constraint:

```text
UNIQUE(rule_set_id, rule_key)
```

`definition` is stored as JSONB.

---

# 12. `runs`

A Run represents a durable workflow execution.

```text
runs
----
id
pile_id
run_type
status
trigger_document_version_id
parent_run_id
baseline_run_id
blocked_on_run_id
started_at
completed_at
created_at
updated_at
```

Run types:

```text
INITIAL
UPDATE
MANUAL
```

Overall Run statuses:

```text
PENDING
RUNNING
REVIEW_REQUIRED
COMPLETED
FAILED
```

Default:

```text
PENDING
```

---

## 12.1 UPDATE trigger invariant

```text
UPDATE
    → trigger_document_version_id MUST be non-null

INITIAL
    → trigger_document_version_id MUST be null

MANUAL
    → trigger_document_version_id MUST be null
```

The database enforces this with `chk_update_trigger`.

Only one UPDATE run may claim a particular trigger document version:

```sql
CREATE UNIQUE INDEX uq_update_trigger_document_version
ON runs (trigger_document_version_id)
WHERE run_type = 'UPDATE';
```

This is a concurrency/idempotency backstop.

---

## 12.2 Run lineage

Runs can reference:

```text
parent_run_id
baseline_run_id
blocked_on_run_id
```

These relationships are distinct and must not be conflated.

The database validates that:

- parent runs belong to the same pile
- baseline runs belong to the same pile
- baseline runs are `COMPLETED`
- blocked-on runs belong to the same pile
- `baseline_run_id` and `blocked_on_run_id` are mutually exclusive

---

## 12.3 Completion invariant

If:

```text
status = COMPLETED
```

then:

```text
completed_at IS NOT NULL
```

is required.

Terminal runs are protected from moving backward:

```text
COMPLETED → anything else
FAILED    → anything else
```

is rejected.

---

# 13. `run_document_versions`

This table records the document versions that belong to a run's input scope.

```text
run_document_versions
---------------------
run_id
document_version_id
created_at
```

Primary key:

```text
PRIMARY KEY(run_id, document_version_id)
```

This is important for complete historical reconstruction.

A document that produces:

- no fact
- no finding
- no evidence

must still be representable as an input to the run.

The V2.7 implementation populates this incrementally as input scope is discovered, together with the relevant document-result/idempotency transaction.

---

# 13A. Additive Run Rule Scope — Migration 002

Migration `002_run_rule_scope.sql` adds immutable Run-owned snapshots of the exact selected RuleSet/Rule scope.

This closes a historical-reconstruction gap that existed in V2.7:

```text
rule_sets
  → belong to pile

rules
  → belong to rule_set

findings.rule_id
  → records only rules that produced Rule-Violation Findings
```

That structure could not prove which rules were examined by a clean / zero-Finding Run, and `rule_sets` / `rules` were mutable source configuration.

The additive model separates:

```text
mutable source rule configuration
        ↓
rule_sets / rules

from

immutable historical Run evaluation scope
        ↓
run_rule_scopes
run_rule_set_snapshots
run_rule_snapshots
```

The copied snapshot content is authoritative for historical reconstruction.

Source IDs remain provenance references only.

---

## 13A.1 `run_rule_scopes`

```text
run_rule_scopes
---------------
run_id
captured_at
created_at
```

Primary key:

```text
PRIMARY KEY(run_id)
```

Foreign key:

```text
run_id -> runs(id) ON DELETE RESTRICT
```

This row is the **scope completion / closure marker**.

Semantics:

```text
no header row
  → rule scope not durably captured

header row + zero children
  → intentionally captured empty rule scope

header row + children
  → captured non-empty rule scope
```

A committed header closes the scope permanently.

The header is inserted **last**.

---

## 13A.2 `run_rule_set_snapshots`

```text
run_rule_set_snapshots
----------------------
run_id
source_rule_set_id
name
description
version
created_at
```

Primary key:

```text
PRIMARY KEY(run_id, source_rule_set_id)
```

Important foreign keys:

```text
source_rule_set_id
  -> rule_sets(id)
  ON DELETE RESTRICT

run_id
  -> run_rule_scopes(run_id)
  ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED
```

The child-to-header cascade exists only inside the closed-scope structure; direct deletion of the closed header is itself rejected by trigger, and the parent Run uses `ON DELETE RESTRICT`.

The deferred header FK allows:

```text
insert RuleSet snapshots
insert Rule snapshots
insert completion header LAST
commit
```

A transaction cannot commit staged children without the completion header.

Copied fields:

```text
name
description
version
```

must exactly match the current source RuleSet at insertion time.

After capture, the copied fields become the historical authority for that Run even if the source RuleSet later changes.

---

## 13A.3 `run_rule_snapshots`

```text
run_rule_snapshots
------------------
run_id
source_rule_id
source_rule_set_id
rule_key
title
description
severity
definition
created_at
```

Primary key:

```text
PRIMARY KEY(run_id, source_rule_id)
```

Important foreign keys:

```text
source_rule_id
  -> rules(id)
  ON DELETE RESTRICT

(run_id, source_rule_set_id)
  -> run_rule_set_snapshots(run_id, source_rule_set_id)
  ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED

run_id
  -> run_rule_scopes(run_id)
  ON DELETE CASCADE
  DEFERRABLE INITIALLY DEFERRED
```

`definition` is copied as JSONB.

Copied fields:

```text
rule_key
title
description
severity
definition
```

must exactly match the mutable source Rule at insertion time.

The snapshot also verifies:

```text
Rule belongs to declared RuleSet
RuleSet belongs to the Run's pile
```

After capture, the snapshot—not the mutable `rules` row—is the authoritative historical content.

---

## 13A.4 Closure and Concurrency

Child snapshot insertion and completion-header insertion serialize on the same authoritative Run row.

Conceptually:

```text
child INSERT
  ↓
SELECT Run FOR UPDATE
  ↓
reject if run_rule_scopes header already exists
  ↓
validate/copy source state

header INSERT
  ↓
SELECT same Run FOR UPDATE
  ↓
close scope
```

This prevents a transaction from committing a child into a scope that another transaction has already closed.

The child/header relationship is also protected by deferred FKs.

---

## 13A.5 Source Locking at Snapshot Insert

Migration 002 validates snapshot content against the source `rule_sets` / `rules` rows under shared row locks.

This protects the copy from racing with concurrent source mutation.

The database therefore verifies:

```text
copied content
    ==
source content at capture time
```

The future application capture service remains responsible for higher-level selection, replay/conflict semantics, and caller-owned transaction sequencing.

---

## 13A.6 Snapshot Immutability

Migration 002 rejects:

```text
UPDATE run_rule_scopes
UPDATE run_rule_set_snapshots
UPDATE run_rule_snapshots
```

It also rejects deletion of committed snapshot children and direct deletion of the completion header.

The parent Run cannot silently erase its rule-scope history because:

```text
run_rule_scopes.run_id -> runs(id) ON DELETE RESTRICT
```

There is no repository delete API for this scope.

---

## 13A.7 Rule-Violation Finding Backstop

Migration 002 adds an insert-time invariant for new `RULE_VIOLATION` Findings.

For:

```text
finding_type = RULE_VIOLATION
rule_id = known non-null Rule
origin_run_id = known Run
```

the origin Run must contain:

```text
run_rule_snapshots.source_rule_id = findings.rule_id
```

This proves that the rule producing the historical Finding was actually present in the origin Run's captured rule scope.

Existing V2.7 behavior remains responsible for:

```text
RULE_VIOLATION + NULL rule_id
unknown rule_id
unknown origin_run_id
```

The migration does not fake-backfill old Runs.

Legacy Runs without a captured rule scope remain historically incomplete for strict later finalization.

---

# 13B. Additive UPDATE Change-Set Review — Migration 003

Migration `003_update_review.sql` adds the durable UPDATE-level approval state absent from V2.7.

```text
update_change_sets
  -> one immutable reviewed change-set per UPDATE Run

update_review_decisions
  -> one immutable APPROVED or REJECTED decision per change-set
```

The change-set binds the exact completed baseline Run, trigger DocumentVersion, Phase 4A preparation fingerprint, deterministic document and Fact deltas, Finding review-state fingerprint, and complete render-input fingerprint. It is not a generic audit/event abstraction.

## 13B.1 Immutability and durable self-consistency

Both tables are insert-only. UPDATE and DELETE are rejected, and parent Run, baseline Run, trigger DocumentVersion, and change-set references use restrictive deletion behavior.

On change-set insert, PostgreSQL locks the Run and requires an UPDATE Run in `REVIEW_REQUIRED`. It verifies that copied baseline/trigger scalar fields equal the Run and that manifest format and scalar identity fields agree. Missing, null, wrongly typed, or unequal manifest values are rejected with `IS DISTINCT FROM`-safe comparisons.

PostgreSQL does not reproduce application canonical JSON serialization or SHA-256. Application paths that trust an existing change-set must parse the stored manifest through the strict domain model, canonically reserialize it, recompute its fingerprint, and require equality with the stored `change_set_fingerprint` before replay, review, or fresh UPDATE finalization.

## 13B.2 Approval and locking

Fresh UPDATE review locks the Run, requires complete Finding review, and inserts one terminal decision plus its audit event in the same transaction. Finding review remains item-level and separate from UPDATE approval.

The approved decision is bound to the full change-set fingerprint, not only the render-input fingerprint. Fresh UPDATE finalization rebuilds the complete change set and requires equality with the approved immutable fingerprint before it can persist output/audit/completion state. A rejected decision cannot finalize the Run.

V2.7 permits only one UPDATE Run for one trigger DocumentVersion. Therefore a rejected UPDATE is terminal for that trigger under the current schema; a later distinct DocumentVersion may create a new UPDATE Run. Rejection does not overload `RunStatus.FAILED`.

## 13B.3 Historical behavior

Migration 003 does not backfill historical UPDATE Runs. A pre-003 UPDATE with no change set/decision is uncaptured and unapproved for fresh strict finalization. Existing completed-output replay remains historical replay behavior and is not rewritten by the migration.

---

# 14. `run_steps`

A Run is decomposed into stage executions.

```text
run_steps
---------
id
run_id
stage
status
attempt
started_at
completed_at
error_code
error_message
created_at
```

Stages:

```text
INGEST
UNDERSTAND
EXAMINE
REVIEW
COMMIT
```

Statuses:

```text
PENDING
RUNNING
AWAITING_UPSTREAM
COMPLETED
RETRYING
SKIPPED
ESCALATED
FAILED
```

Constraint:

```text
UNIQUE(run_id, stage, attempt)
```

This allows retries to be represented explicitly:

```text
EXAMINE attempt 1
EXAMINE attempt 2
EXAMINE attempt 3
```

---

# 15. Run-Step State Machine

The database transition rules are:

```text
PENDING
  → RUNNING

RUNNING
  → COMPLETED
  → RETRYING
  → SKIPPED
  → ESCALATED
  → FAILED
  → AWAITING_UPSTREAM

AWAITING_UPSTREAM
  → RUNNING

RETRYING
  → RUNNING
```

Same-status transitions are idempotent.

Terminal step states do not have outgoing transitions in the database trigger.

Important:

```text
PENDING → SKIPPED
```

is not a valid transition under the locked DDL.

---

# 16. `facts`

Facts represent extracted/validated factual state.

```text
facts
-----
id
origin_run_id
subject
predicate
object_value
status
created_at
```

Fact statuses:

```text
CONFIRMED
DISPUTED
SUPERSEDED
```

The originating run is immutable.

Fact content and status are protected by database-level immutability.

New factual state is represented through new fact rows/membership rather than rewriting historical facts.

---

# 17. `run_facts`

This table separates fact identity/origin from run membership.

```text
run_facts
---------
run_id
fact_id
relation
created_at
```

Relation:

```text
CREATED
CARRIED_FORWARD
```

Primary key:

```text
PRIMARY KEY(run_id, fact_id)
```

This allows a later run to carry forward an existing fact without mutating the originating fact.

Example:

```text
RUN 1
  F1
  F2

RUN 2
  F1 → CARRIED_FORWARD
  F3 → CREATED
```

---

# 18. `fact_evidence`

Associates facts with supporting evidence spans.

```text
fact_evidence
-------------
fact_id
evidence_span_id
```

Primary key:

```text
PRIMARY KEY(fact_id, evidence_span_id)
```

All referenced evidence is protected by `ON DELETE RESTRICT`.

---

# 19. `findings`

Findings represent reviewable issues.

```text
findings
--------
id
origin_run_id
rule_id
finding_type
status
title
description
severity
created_at
updated_at
```

Finding types:

```text
RULE_VIOLATION
CONTRADICTION
```

Finding statuses:

```text
PENDING
APPROVED
REJECTED
```

Rule relationship:

```text
RULE_VIOLATION
    → rule_id MUST NOT be null

CONTRADICTION
    → rule_id MUST be null
```

This is enforced by `chk_finding_rule_relationship`.

Migration 002 adds an additional provenance backstop for newly inserted Rule-Violation Findings:

```text
known non-null findings.rule_id
    ↓
must exist in run_rule_snapshots for findings.origin_run_id
```

This binds the Finding to the immutable Rule snapshot evaluated by its origin Run rather than relying only on the current mutable `rules` row.

---

# 20. Finding Immutability

A finding's historical content is immutable.

The database protects:

```text
origin_run_id
rule_id
finding_type
title
description
severity
```

Once a finding is terminal:

```text
APPROVED
REJECTED
```

its decision cannot be changed to the opposite terminal decision.

The finding immutability trigger is a database backstop in addition to domain-level immutability.

---

# 21. `run_findings`

This table separates finding origin from run membership.

```text
run_findings
------------
run_id
finding_id
relation
created_at
```

Relation:

```text
CREATED
CARRIED_FORWARD
```

Primary key:

```text
PRIMARY KEY(run_id, finding_id)
```

This makes incremental carry-forward explicit and reconstructable.

Example:

```text
RUN 1
  F1
  F2

RUN 2
  F1 → CARRIED_FORWARD
  F3 → CREATED
```

The original finding remains associated with its `origin_run_id`.

---

# 22. `finding_evidence`

Associates findings with supporting evidence spans.

```text
finding_evidence
----------------
finding_id
evidence_span_id
```

Primary key:

```text
PRIMARY KEY(finding_id, evidence_span_id)
```

This preserves explicit finding-to-evidence provenance.

---

# 23. `review_decisions`

Human review decisions are stored as historical events.

```text
review_decisions
----------------
id
finding_id
decision
actor_type
actor_id
created_at
idempotency_key
```

Decision values:

```text
APPROVED
REJECTED
```

Constraint:

```text
UNIQUE(idempotency_key)
```

The authoritative current decision state remains:

```text
findings.status
```

while `review_decisions` preserves decision history.

---

# 24. Approval Transaction

Approval/rejection is designed as a transactionally serialized operation.

Conceptually:

```text
BEGIN
  ↓
SELECT run FOR UPDATE
  ↓
Read finding state
  ↓
Validate decision
  ↓
Update finding
  ↓
Insert review_decision
  ↓
Check remaining PENDING findings
  ↓
If complete:
    apply commit
    write output/audit state
  ↓
COMMIT
```

The application must retry the **entire approval transaction** after PostgreSQL deadlock or serialization failure.

It must not retry only the final commit operation.

---

# 25. `outputs`

Outputs are durable generated deliverables.

```text
outputs
-------
id
run_id
output_type
storage_uri
content_hash
created_at
```

Constraint:

```text
UNIQUE(run_id, output_type)
```

There is no `version` column.

A run has one canonical output of a given type.

Historical output versions are represented by separate immutable runs.

---

# 26. `idempotency_records`

This table prevents duplicate expensive logical operations.

```text
idempotency_records
-------------------
id
scope
key
status
result_reference
claimed_at
created_at
completed_at
```

Statuses:

```text
IN_PROGRESS
COMPLETED
FAILED
```

Constraint:

```text
UNIQUE(scope, key)
```

The logical operation identity can encode:

```text
(run_id, stage, document_version_id, operation_name)
```

The expensive LLM/provider call must not hold a database transaction open.

The intended pattern is:

```text
CLAIM
  ↓
LLM / provider call
  ↓
BEGIN
  ├── persist result
  ├── persist domain state
  ├── persist run_document_versions membership
  └── mark idempotency COMPLETED
  ↓
COMMIT
```

If a worker dies while the record is `IN_PROGRESS`, `claimed_at` supports stale-claim detection and safe reclamation.

---

# 27. `audit_events`

Audit events provide a durable history of important operations.

```text
audit_events
------------
id
run_id
entity_type
entity_id
event_type
actor_type
actor_id
payload
created_at
```

`payload` is JSONB.

Audit records are historical and should not be destructively cascaded away.

---

# 28. `run_metrics`

Metrics are stored per stage and per attempt.

```text
run_metrics
-----------
id
run_id
stage
attempt
duration_ms
input_tokens
output_tokens
estimated_cost
created_at
```

Constraint:

```text
UNIQUE(run_id, stage, attempt)
```

This makes retry cost/time accounting explicit.

Example:

```text
EXAMINE attempt 1 → $0.03
EXAMINE attempt 2 → $0.02
EXAMINE attempt 3 → $0.01
```

Metrics must not overwrite one attempt with another.

---

# 29. Vector Search

`document_chunks.embedding` uses pgvector.

The locked V2.7 configuration uses:

```text
vector(1536)
```

and:

```text
HNSW
vector_cosine_ops
```

Conceptually:

```text
document_chunks.embedding
        ↓
HNSW index
        ↓
cosine similarity search
```

The embedding dimension must match the selected embedding model.

---

# 30. Historical Deletion Policy

Historical data is protected from destructive parent cascades.

Foreign keys for historical entities use:

```text
ON DELETE RESTRICT
```

where specified by the executable migrations.

Migration 002 follows this policy at the Run boundary:

```text
run_rule_scopes.run_id -> runs(id) ON DELETE RESTRICT
```

The snapshot child tables use internal `ON DELETE CASCADE` links to their scope/header structure, but direct deletion of the closed header or committed children is rejected by database triggers. This does not permit parent-Run deletion to erase historical scope.

The purpose is to prevent deletion of parent entities from silently destroying:

- runs
- findings
- facts
- evidence
- review decisions
- outputs
- audit events
- metrics
- provenance

Historical reconstruction takes priority over convenient cascading deletion.

---

# 31. Core Integrity Constraints

The locked schema relies on several categories of database enforcement.

## Primary keys

Every major entity has a UUID primary key.

Junction tables use composite primary keys where appropriate.

## Foreign keys

Relationships are explicitly represented through foreign keys.

## Unique constraints

Important examples:

```text
documents:
  UNIQUE(pile_id, logical_key)

document_versions:
  UNIQUE(document_id, version_number)
  UNIQUE(document_id, content_hash)

document_chunks:
  UNIQUE(document_version_id, chunk_index)
  UNIQUE(id, document_version_id)

rules:
  UNIQUE(rule_set_id, rule_key)

runs:
  partial UNIQUE(trigger_document_version_id)
  WHERE run_type = UPDATE

run_document_versions:
  PRIMARY KEY(run_id, document_version_id)

run_steps:
  UNIQUE(run_id, stage, attempt)

run_facts:
  PRIMARY KEY(run_id, fact_id)

fact_evidence:
  PRIMARY KEY(fact_id, evidence_span_id)

run_findings:
  PRIMARY KEY(run_id, finding_id)

finding_evidence:
  PRIMARY KEY(finding_id, evidence_span_id)

review_decisions:
  UNIQUE(idempotency_key)

outputs:
  UNIQUE(run_id, output_type)

idempotency_records:
  UNIQUE(scope, key)

run_metrics:
  UNIQUE(run_id, stage, attempt)

run_rule_scopes:
  PRIMARY KEY(run_id)

run_rule_set_snapshots:
  PRIMARY KEY(run_id, source_rule_set_id)

run_rule_snapshots:
  PRIMARY KEY(run_id, source_rule_id)

update_change_sets:
  UNIQUE(run_id)

update_review_decisions:
  UNIQUE(change_set_id)
  UNIQUE(idempotency_key)
```

---

# 32. Evidence Integrity

Evidence provenance is a critical integrity boundary.

The schema prevents this invalid state:

```text
evidence_spans.document_version_id = V1
             +
evidence_spans.chunk_id → chunk belonging to V2
```

The composite foreign key guarantees:

```text
(chunk_id, document_version_id)
        ↓
document_chunks(id, document_version_id)
```

Therefore the quoted evidence is tied to the same document version represented by the evidence row.

---

# 33. Current-Version Integrity

`documents.current_version_id` is not merely a pointer.

The database verifies that the referenced version:

1. exists
2. belongs to the same document
3. has `processing_status = READY`
4. has a version number greater than the previous current version

Application-side version creation also serializes on:

```sql
SELECT ... FOR UPDATE
```

of the parent document.

This protects:

- version numbering
- current-version advancement
- concurrent version creation

---

# 34. Run Lineage Integrity

Run lineage is validated at the database level.

Rules include:

```text
parent_run_id
  → same pile

baseline_run_id
  → same pile
  → COMPLETED

blocked_on_run_id
  → same pile

baseline_run_id
  XOR
blocked_on_run_id
```

The database also prevents terminal Runs from being moved backward.

---

# 35. Concurrency Model

The schema supports several important concurrency boundaries.

## Same document

```text
Worker A ──┐
           ├── SELECT document FOR UPDATE
Worker B ──┘
```

Only one transaction computes/updates the current version at a time.

## Same UPDATE delta

```text
Worker A ──┐
           ├── partial UNIQUE UPDATE claim
Worker B ──┘
```

Only one UPDATE run can claim the same trigger document version.

## Same Run approval

```text
Approval A ──┐
             ├── Run FOR UPDATE
Approval B ──┘
```

Approval transactions are serialized around the Run.

## Same Run rule-scope closure

```text
Child capture ──┐
                ├── Run FOR UPDATE
Scope close  ───┘
```

Migration 002 uses the same Run row as the serialization primitive for child snapshot insertion and completion-header insertion.

This ensures a committed closed scope cannot later grow through a concurrent child insert.

## Same idempotent operation

```text
Worker A ──┐
           ├── UNIQUE(scope, key)
Worker B ──┘
```

Only one logical operation claim succeeds.

---

# 36. Historical Reconstruction

The schema is intentionally designed so a historical run can answer:

```text
WHAT WAS INPUT?
        ↓
run_document_versions

WHAT FACTS EXISTED?
        ↓
run_facts → facts

WHAT FINDINGS EXISTED?
        ↓
run_findings → findings

WHAT RULE SCOPE WAS ACTUALLY CAPTURED?
        ↓
run_rule_scopes
        ↓
run_rule_set_snapshots / run_rule_snapshots

WHAT RULE CONTENT DID THE RUN EVALUATE?
        ↓
immutable copied RuleSet/Rule snapshot fields

WHICH WERE CARRIED FORWARD?
        ↓
relation

WHAT EVIDENCE SUPPORTED THEM?
        ↓
finding_evidence / fact_evidence
        ↓
evidence_spans
        ↓
document_versions

WHAT DID THE HUMAN DECIDE?
        ↓
review_decisions

WHAT UPDATE CHANGE SET WAS REVIEWED?
        ↓
update_change_sets.manifest
        ↓
update_change_sets.change_set_fingerprint

WHAT WAS THE UPDATE-LEVEL DECISION?
        ↓
update_review_decisions

WHAT OUTPUT WAS PRODUCED?
        ↓
outputs

WHAT HAPPENED AND WHEN?
        ↓
audit_events

HOW MUCH DID IT COST?
        ↓
run_metrics
```

This separation is essential for reproducibility and auditability.

---

# 37. Origin vs Membership

Facts and findings deliberately separate:

```text
origin
```

from:

```text
run membership
```

For example:

```text
facts.origin_run_id
run_facts.run_id
run_facts.relation
```

and:

```text
findings.origin_run_id
run_findings.run_id
run_findings.relation
```

This allows:

```text
Run 1 creates F1

Run 2 carries F1 forward
```

without changing F1's historical origin.

This is a core part of incremental-update semantics.

---

# 38. No Mutable Historical Rewrites

Historical entities are designed around append/history semantics.

Do not mutate a historical fact/finding simply because a later run produces different information.

Instead:

```text
new run
  ↓
new fact/finding state
  +
membership/carry-forward relations
```

Terminal finding decisions and fact history are protected by database triggers.

---

# 39. Schema-Level vs Application-Level Responsibilities

Not every rule belongs in SQL.

### Database responsibilities

Examples:

- PK/FK integrity
- uniqueness
- enum types
- CHECK constraints
- evidence/version consistency
- current-version validity
- run lineage
- run-step transitions
- terminal status protection
- immutable historical content
- UPDATE claim uniqueness
- deletion restrictions
- immutable Run rule-scope closure
- exact RuleSet/Rule snapshot-copy validation
- RuleSet-to-Run pile consistency
- Rule-to-RuleSet consistency
- Rule-Violation Finding origin-snapshot backstop

### Application responsibilities

Examples:

- orchestration
- workflow execution
- LLM/provider calls
- retry strategy
- deadlock/serialization retry
- transaction sequencing
- human approval use cases
- determining workflow scope
- deciding when a run should be queued
- deciding when an operation is stale/reclaimable

The schema should enforce durable invariants without turning PostgreSQL into the workflow engine.

---

# 40. Locked Design Rules

Unless the approved Engineer specification explicitly changes them:

1. PostgreSQL is the durable authoritative state store.
2. The V2.7 DDL is the executable schema contract.
3. Run and Run-step status are separate concepts.
4. `AWAITING_UPSTREAM` and `RETRYING` belong to `run_step_status`, not `run_status`.
5. Facts are historical/immutable.
6. Finding content is immutable.
7. Terminal finding decisions are immutable.
8. Carry-forward is represented through membership tables.
9. Evidence must be tied to the exact document version it claims to support.
10. Complete run input scope is represented through `run_document_versions`.
11. LLM operations use idempotency records rather than holding database transactions open.
12. Retry attempts are represented explicitly in both `run_steps` and `run_metrics`.
13. Outputs are unique per `(run_id, output_type)`.
14. Historical deletion uses restrictive foreign-key behavior.
15. PostgreSQL row locking is used for concurrency-sensitive durable operations.
16. Schema changes should be additive/intentional and reviewed against the locked architecture.
17. Migration `001_schema_v2_7.sql` remains the locked historical baseline and must not be rewritten.
18. The current executable schema also includes reviewed additive migration `002_run_rule_scope.sql`.
19. A Run's evaluated rule scope is represented by immutable Run-owned snapshots, not inferred from current mutable Rule rows.
20. `run_rule_scopes` distinguishes uncaptured scope from intentionally captured empty scope.
21. A committed Run rule scope is closed: snapshot membership cannot later grow or shrink.
22. New Rule-Violation Findings must be backed by the corresponding immutable Rule snapshot in their origin Run.
23. Migration `003_update_review.sql` adds one immutable change set per UPDATE Run and one immutable UPDATE-level review decision per change set.
24. Fresh UPDATE finalization requires an immutable APPROVED update-review decision and exact reconstruction of its full approved change-set fingerprint.
25. UPDATE change-set and review-decision insertion lock the associated Run and reject stale or non-reviewable state; they do not replace Finding-level review.
26. Migration `004_idempotency_claim_generation.sql` fences stale idempotency owners with immutable claim generations and PostgreSQL-authoritative claim timestamps.
27. Migration `005_run_step_retry_history.sql` records semantic retry attempts explicitly and requires exactly one metric for every closed RunStep attempt.
28. Migration `006_document_runtime_commit_and_telemetry.sql` marks every historical nonterminal and every new Run as `commit_required = TRUE`; only Runs already COMPLETED when 006 is applied receive the immutable compatibility value `FALSE`.
29. A commit-required Run can create a fresh `FINAL_REPORT` only while its latest COMMIT attempt is RUNNING, and it can transition to COMPLETED only after a `FINAL_REPORT` exists and its latest COMMIT attempt is COMPLETED.
30. Migration 006 never fabricates historical COMMIT RunSteps or provider-usage receipts; pre-006 active four-stage Runs are reconciled by the application to one pending COMMIT before fresh completion, while historical completed-output replay remains read-only.
31. `provider_usage_events` records one provider invocation receipt per RunStep/operation/generation; STARTED and UNAVAILABLE carry no token/cost values, while RECORDED preserves whatever usage fields are truthfully known.
32. READY and FAILED DocumentVersions are terminal immutable history, `piles.latest_run_id` must reference the same Pile, and an established `parent_run_id` cannot be mutated.

---

# 41. Relationship Summary

```text
PILE
 ├──< DOCUMENT
 │      └──< DOCUMENT_VERSION
 │              ├──< DOCUMENT_CHUNK
 │              └──< EVIDENCE_SPAN
 │
 ├──< RULE_SET
 │      └──< RULE
 │
 └──< RUN
        ├──< RUN_DOCUMENT_VERSION >── DOCUMENT_VERSION
        ├──< RUN_RULE_SCOPE
        │      ├──< RUN_RULE_SET_SNAPSHOT >── source RULE_SET
        │      └──< RUN_RULE_SNAPSHOT >───── source RULE
        │
        ├──< RUN_STEP
        ├──< RUN_FACT >── FACT
        │                  └──< FACT_EVIDENCE >── EVIDENCE_SPAN
        │
        ├──< RUN_FINDING >── FINDING
        │                     ├──< FINDING_EVIDENCE >── EVIDENCE_SPAN
        │                     └──< REVIEW_DECISION
        │
        ├──< OUTPUT
        ├──< IDEMPOTENCY_RECORD
        ├──< AUDIT_EVENT
        ├──< RUN_METRIC
        └──< PROVIDER_USAGE_EVENT
```

---

The UPDATE-only review relationship is:

```text
RUN -> UPDATE_CHANGE_SET -> UPDATE_REVIEW_DECISION
```

# 42. Final Schema Contract

The locked schema is intentionally designed around four goals:

```text
1. Durable workflow state
2. Historical reconstruction
3. Evidence/provenance integrity
4. Concurrency/idempotency safety
```

The schema should support questions such as:

```text
What documents did this run actually process?

Which version of each document was used?

Which evidence supports this finding?

Which run originally created this finding?

Was this finding carried forward?

What did the human decide?

What output was produced?

Which RuleSets and Rules were captured for this Run?

What exact Rule/RuleSet content was evaluated historically?

Was the Run rule scope intentionally empty or never captured?

Can a Rule-Violation Finding be traced to a rule actually captured by its origin Run?

What happened during the run?

How many attempts did a stage require?

How much did the run cost?

Can two concurrent workers create duplicate logical work?

Can historical state be silently overwritten?
```

The database design exists to make those answers structurally reconstructable rather than dependent entirely on application convention.

---

# 42A. Migration History and Current Baseline

## Migration 001

```text
001_schema_v2_7.sql
```

Status:

```text
LOCKED HISTORICAL BASELINE
```

It defines the original V2.7 21-table schema.

Do not rewrite it to incorporate later discoveries.

## Migration 002

```text
002_run_rule_scope.sql
```

Status:

```text
APPROVED ADDITIVE CORRECTION
```

It adds:

```text
run_rule_scopes
run_rule_set_snapshots
run_rule_snapshots
```

and the Rule-Violation Finding origin-snapshot insert backstop.

The reason for migration 002 is a verified historical-provenance gap, not convenience:

```text
V2.7 could not prove the exact rules examined by a historical Run,
especially when no Rule-Violation Finding was produced.
```

Current implemented repository/application baseline after migrations 001 + 002:

```text
340 tests passing
```

A database used by repository/application tests must apply migrations in order before running tests that depend on Run rule scope.

---

## Migration 003

```text
003_update_review.sql
```

Status:

```text
IMPLEMENTED REVIEWED ADDITIVE UPDATE-APPROVAL CORRECTION — pending independent checkpoint approval
```

It adds:

```text
update_change_sets
update_review_decisions
```

and the immutable full-state approval binding required for UPDATE finalization. The current executable schema after migrations 001, 002, and 003 contains 26 tables.

Migration 003 is additive. It does not alter V2.7's one-UPDATE-per-trigger constraint and does not backfill historical UPDATE Runs.

---

# 43. Important Boundary

This document defines the **data model and database invariants**.

It does not define:

- REST endpoint contracts
- MCP tool contracts
- LangGraph node implementation
- worker implementation
- LLM prompts
- frontend implementation
- repository class APIs
- application service APIs

Those belong to their respective architectural layers.

The executable database contract is the ordered migration set:

```text
sql/migrations/001_schema_v2_7.sql
sql/migrations/002_run_rule_scope.sql
sql/migrations/003_update_review.sql
sql/migrations/004_idempotency_claim_generation.sql
sql/migrations/005_run_step_retry_history.sql
sql/migrations/006_document_runtime_commit_and_telemetry.sql
```

Migration `001` remains the locked historical V2.7 baseline.

Migration `002` is the approved additive rule-scope provenance correction.

Migration `003` is the implemented/reviewed additive UPDATE change-set and immutable approval correction, pending independent checkpoint approval.

Migration `004` adds fenced idempotency claim ownership without adding tables.

Migration `005` adds explicit retry-attempt provenance and closed-attempt metric integrity without adding tables.

Migration `006` adds the durable document-runtime/COMMIT cutover and provider-usage receipt model, bringing the ordered executable schema to 27 tables.

Migrations `001` through `005` are historical migration artifacts and are not rewritten by the Phase 6A correction. Future changes must continue additively unless an explicitly reviewed migration policy says otherwise.

---

# 44. Migration 004: Fenced Idempotency Claim Takeover

Migration `004_idempotency_claim_generation.sql` is additive and changes the
existing `idempotency_records` table only. The executable schema therefore
remains **26 tables**. It fails closed if a legacy `IN_PROGRESS` record has no
`claimed_at`; migration never fabricates a historical lease timestamp.

`claim_generation BIGINT` is the durable ownership and fencing token. Legacy
pre-004 records are backfilled to generation `0`; new claims use the permanent
database default generation `1`. The column is non-null and non-negative.

`claimed_at` is solely the PostgreSQL-authoritative lease/staleness clock.
Every `IN_PROGRESS` record must have a non-null value. A stale takeover uses
the inclusive predicate `claimed_at <= clock_timestamp() - stale_after`,
increments `claim_generation`, and refreshes `claimed_at` atomically.
Completion and failure require the currently owned generation; a stale owner
cannot complete or fail a replacement generation.

---

# 45. Migration 005: Explicit Retry Attempt History

Migration `005_run_step_retry_history.sql` is additive and adds no tables; the
executable schema remains **26 tables**. It records retryable provider/result
failure classification on `idempotency_records`, while preserving null
classification for historical failed records whose cause cannot be truthfully
reconstructed.

Retries are distinct `run_steps` attempts, limited durably to attempts 1–3.
The predecessor is closed as `RETRYING`, retains immutable operation, failed
generation, classification, database completion time, and a truthful metric;
the successor carries immutable retry provenance. `RETRYING` is historical and
has no outgoing state transition. Attempt three retry exhaustion is
`ESCALATED`, with the Run remaining `RUNNING` for explicit intervention.

Every closed RunStep has database-authoritative valid start/completion
timestamps and exactly one `run_metrics` record. Metric duration is derived as
`FLOOR(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000)::BIGINT`; token
and cost fields are never fabricated by migration. A composite metric foreign
key and deferred closure check prevent orphan/open metrics and unmeasured
closed attempts.

---

# 46. Migration 006: Document Runtime, COMMIT Authority, and Provider Usage

Migration `006_document_runtime_commit_and_telemetry.sql` is additive. It adds
`runs.commit_required`, the `provider_usage_state` enum, and the
`provider_usage_events` table. The executable schema after ordered migrations
`001` through `006` contains **27 tables**. Migrations `001` through `005`
remain unchanged historical artifacts.

## 46.1 Durable COMMIT cutover

`commit_required` is the permanent Phase-6 completion-authority marker:

- Runs already `COMPLETED` when migration 006 is applied are assigned
  `commit_required = FALSE` so their existing output can remain readable and
  replayable without inventing COMMIT history.
- Every historical nonterminal Run is assigned `commit_required = TRUE`.
- Every Run created after migration 006 defaults to `commit_required = TRUE`.
- The marker is `NOT NULL` and immutable after cutover. Application callers
  cannot create a new compatibility Run or downgrade a Phase-6 Run to `FALSE`.
- Migration 006 does **not** fabricate COMMIT RunSteps for any historical Run.

There is no generic fresh direct-finalization compatibility path. For a
commit-required Run, inserting a fresh `FINAL_REPORT` requires the Run to be
`REVIEW_REQUIRED` and the latest COMMIT attempt to be `RUNNING`. A subsequent
transition to `COMPLETED` requires a `FINAL_REPORT` and the latest COMMIT
attempt to be `COMPLETED`.

Historical Runs that were already `COMPLETED` before migration 006 are a
read-only compatibility class only. `FinalizeRunService` may validate/replay an
already committed historical output, but it cannot create a fresh output for
that class. A pre-006 **active** four-stage Run is not direct-finalization
compatible: `RunWorkflowService` reconciles it to exactly one pending COMMIT
attempt before it can complete through the normal five-stage workflow.

`CommitFinalReportService` is therefore the sole public fresh-finalization
authority. Its generation-fenced transaction verifies current review/render
state, the deterministic final-report bytes and artifact hash, the active
COMMIT attempt, and the current idempotency owner before persisting Output,
audit, metric, claim completion, COMMIT completion, and finally Run completion.

## 46.2 Provider usage receipts and metrics

`provider_usage_events` is a durable receipt for an actual provider invocation,
keyed by `(run_id, stage, attempt, operation_scope, operation_key,
claim_generation)`. It is tied to the semantic RunStep attempt, not merely the
current idempotency row. Exact service replay creates no new receipt; a Phase
5B generation takeover receives its own receipt.

Receipt state semantics are exact:

- `STARTED`: `recorded_at`, token counts, and estimated cost are null.
- `UNAVAILABLE`: `recorded_at` is non-null and token/cost fields are null.
- `RECORDED`: `recorded_at` is non-null; truthfully known token/cost fields are
  stored and fields the provider did not supply may remain null.

Provider and model identity are durable. Optional configured model pricing may
produce a `NUMERIC(18,8)` Decimal cost estimate; when pricing or usage is not
known, cost remains null rather than being invented. RunStep metric aggregation
uses only receipts belonging to that exact attempt, so retry successors and
Phase-5B generations do not double-count earlier invocations.

## 46.3 Document-runtime and lineage backstops

Migration 006 makes both `READY` and `FAILED` DocumentVersions immutable
terminal history. A failed `(document_id, content_hash)` is not silently
reopened under a new lifecycle. Content-addressed source bytes outside
PostgreSQL are non-authoritative orphans until a READY DocumentVersion commits.

`piles.latest_run_id` is the authoritative automatic-arrival lineage tail and
must reference a Run in the same Pile. The application holds the Pile row lock
while advancing that tail; completion of an older Run cannot move it backward.
An established `runs.parent_run_id` is immutable. Existing invalid cross-pile
tails fail migration rather than being silently rewritten.

## 46.4 Focused UPDATE final artifacts

For an UPDATE, the fresh finalization path reads and verifies the exact baseline
`FINAL_REPORT` artifact and builds the new report through focused byte
patching. Retained document lines, carried Finding lines, unchanged evidence
citation lines, and the unchanged portions of carried Fact claim lines are
copied from baseline bytes; only material that the approved UPDATE state says
changed is regenerated. The patched bytes must still equal the canonical
report for the frozen current input.

The `FINAL_OUTPUT_COMMITTED` audit payload carries a deterministic
`focused_update` proof containing the baseline Run/Output identity, baseline
content hash, total preserved byte count, and every preserved baseline/updated
byte range with its SHA-256 hash. Replay recomputes and validates that proof.
This preservation proof is an application-level finalization invariant; the
Migration-006 SQL triggers remain responsible for the durable Output/COMMIT/Run
state gate.

---

Phase 6A also keeps the narrow PostgreSQL-authoritative workflow amendment in
`.codex-context/ARCHITECTURE_DESIGN.md`: `RunWorkflowService` is the implemented
hand-rolled orchestration authority for this checkpoint, with no LangGraph,
Redis, queue, scheduler, or second durable workflow-state store introduced.
