# Phase 6B — External Interface Contract and Implementation Record

## Status

Implemented through Phase 6B-6; Phase 6B-7 performs final documentation, hardening, and closure verification.

Phase 6A remains the application/runtime core. Phase 6B exposes that core through thin REST and MCP boundaries plus a React review UI. PostgreSQL remains authoritative; REST, MCP, and the browser do not duplicate workflow or approval logic.

## 1. Goals

Phase 6B-1 locks the external contract before endpoint implementation:

1. versioned REST namespace and resource model;
2. stable request/response/error envelopes;
3. REST-to-application-service ownership;
4. MCP tool names and parity with REST capabilities;
5. review/idempotency semantics;
6. implementation gaps that need small application/query facades in 6B-2/6B-3;
7. test strategy for the transport layer.

This checkpoint does **not** add Redis, workers, LangGraph, a watcher daemon, React, authentication, or a second workflow implementation.

## 2. Fixed architectural rules

- Base REST prefix: `/api/v1`.
- Existing `/health` remains unversioned for infrastructure probes.
- REST handlers validate transport input, invoke application/use-case services, serialize output, and map typed application errors.
- REST handlers do not execute SQL directly.
- MCP tools call the same application/use-case layer as REST.
- MCP does not call REST over HTTP internally.
- Domain modules remain free of FastAPI/MCP dependencies.
- PostgreSQL remains the durable source of truth.
- Review/approval remains explicit and cannot be inferred from a successful read or upload.
- COMMIT remains the only fresh final-output completion path.

## 3. External resource model

The public model exposes these durable concepts:

- Pile
- RuleSet / Rule
- Document / DocumentVersion
- Run / RunStep
- Fact / Evidence
- Finding / Evidence
- ReviewDecision
- UpdateChangeSet / UpdateReviewDecision
- FinalReport Output
- RunMetric

Internal idempotency rows, provider usage-event rows, raw workflow claims, and database locking details are not first-class public resources.

## 4. REST v1 contract

### 4.1 Infrastructure

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process/database health probe (existing endpoint) |

### 4.2 Piles

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/piles` | Create a pile |
| GET | `/api/v1/piles/{pile_id}` | Read pile state including authoritative `latest_run_id` |
| GET | `/api/v1/piles/{pile_id}/documents` | List logical documents and current version IDs |
| GET | `/api/v1/piles/{pile_id}/runs` | List durable runs for the pile |

`POST /piles` request:

```json
{
  "name": "Acme vendor file",
  "description": "Synthetic contract corpus",
  "watch_enabled": false,
  "watch_location": null
}
```

`watch_*` fields are durable configuration only in 6B; a watcher daemon is deferred to 6C.

### 4.3 Rules

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/piles/{pile_id}/rule-sets` | Create one rule-set version with its rules atomically |
| GET | `/api/v1/piles/{pile_id}/rule-sets` | List rule sets available for scope capture |
| GET | `/api/v1/rule-sets/{rule_set_id}` | Read one rule set and its rules |

Rule-set creation is needed because the Engineer flow says the user supplies the rules to evaluate; the current Phase-6A repository only has read-side rule-source access.

Request shape:

```json
{
  "name": "Contract playbook",
  "description": "Rules used for the run",
  "version": 1,
  "rules": [
    {
      "rule_key": "payment-term",
      "title": "Payment term maximum",
      "description": "Flag terms longer than allowed",
      "severity": "HIGH",
      "definition": {"max_days": 45}
    }
  ]
}
```

### 4.4 Documents and ingestion

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/piles/{pile_id}/documents` | Ingest one source file and reconcile automatic Run lineage |
| GET | `/api/v1/documents/{document_id}` | Read logical document state |
| GET | `/api/v1/documents/{document_id}/versions` | List immutable historical versions |

Upload is `multipart/form-data`:

- `logical_key`: required string identifying the logical document;
- `file`: required binary PDF/DOCX/TXT source.

Successful response contains both ingestion and arrival information so a machine can continue without guessing:

```json
{
  "document_id": "...",
  "document_version_id": "...",
  "content_hash": "sha256:...",
  "replayed": false,
  "created_run_ids": ["..."],
  "reconciled_blocked_run_id": null
}
```

This requires a narrow application facade in 6B-2 that composes `IngestDocumentService` and `DocumentArrivalService`. The REST handler itself must not reproduce that orchestration.

Exact historical READY replay remains valid even when a newer version is current; the response may therefore report `replayed=true` without creating a new Run.

### 4.5 Runs and visible stages

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/runs/{run_id}` | Read Run plus complete ordered RunStep attempt history |
| POST | `/api/v1/runs/{run_id}/advance` | Advance/reconcile exactly one durable workflow stage |
| GET | `/api/v1/runs/{run_id}/facts` | Read run-local Facts with evidence IDs |
| GET | `/api/v1/runs/{run_id}/findings` | Read run-local Findings with evidence IDs |
| GET | `/api/v1/runs/{run_id}/metrics` | Read stage/attempt timing, token and cost metrics |

Advance request:

```json
{
  "source_rule_set_ids": ["..."],
  "source_rule_ids": ["..."]
}
```

Both fields are nullable together. The first scope-capturing advance that needs rules supplies both; subsequent exact replays may omit them only where the application service permits it.

Advance response:

```json
{
  "run_id": "...",
  "stage": "EXAMINE",
  "attempt": 2,
  "step_status": "COMPLETED",
  "run_status": "RUNNING",
  "outcome": "COMPLETED",
  "advanced": true
}
```

`outcome` uses the application outcomes `COMPLETED`, `REPLAYED`, or `RETRY_SCHEDULED`.

### 4.6 Human review

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/runs/{run_id}/review` | Read the complete frozen review payload needed by a human or machine driver |
| POST | `/api/v1/runs/{run_id}/findings/{finding_id}/decision` | Approve/reject one Finding |
| POST | `/api/v1/runs/{run_id}/update/decision` | Approve/reject the UPDATE change set |

Finding decision request:

```json
{
  "decision": "APPROVED",
  "actor_type": "HUMAN",
  "actor_id": "reviewer@example.test",
  "idempotency_key": "review:<run-uuid>:<caller-token>"
}
```

The same review may contain both approved and rejected Findings. A decision only resolves the addressed Finding.

UPDATE decision request:

```json
{
  "decision": "APPROVED",
  "actor_type": "HUMAN",
  "actor_id": "reviewer@example.test",
  "idempotency_key": "update-review:<run-uuid>:<caller-token>",
  "expected_change_set_fingerprint": "sha256:..."
}
```

The review GET response contains enough immutable IDs/provenance for the UI/MCP client to render evidence and make explicit decisions. It does not manufacture approval state. For an UPDATE whose finding-level review is complete but whose change set has not yet been persisted, the response may include a deterministic read-only change-set preview with `durable=false`, `change_set_id=null`, and its `change_set_fingerprint`. The UPDATE decision must echo that fingerprint. The application facade persists/replays the durable change set and rejects the decision with a stale-review conflict if the authoritative fingerprint no longer matches what the caller reviewed.

### 4.7 Final report

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/runs/{run_id}/report` | Read final-output metadata |
| GET | `/api/v1/runs/{run_id}/report/content` | Stream verified committed Markdown bytes |

There is no REST endpoint that directly writes a FINAL_REPORT Output. The Run is advanced through its durable COMMIT stage via `POST /api/v1/runs/{run_id}/advance`; the existing COMMIT application service remains the sole fresh finalizer. Report content is read only after durable COMPLETED/output/audit proof is verified, and artifact bytes are hash-verified before they are returned.

For UPDATE runs, report metadata includes the durable focused-update preservation proof carried by the finalization audit payload (or a stable read projection of it), without changing the proof semantics.

## 5. Common response conventions

### Identifiers and timestamps

- UUIDs are canonical lowercase strings.
- timestamps are RFC 3339 / ISO-8601 with timezone;
- enum values are the exact durable uppercase values already used by the domain/database.

### Pagination

6B starts with deterministic bounded lists. Cursor pagination is deferred unless a list can grow enough to make an unbounded response unsafe. Endpoints implemented in 6B must define an explicit maximum `limit` even when the prototype UI normally requests fewer rows.

### No silent success

A response that says a Run is `COMPLETED`, a Finding is resolved, or a final report exists must be derived from durable state. Artifact existence alone is never sufficient.

## 6. Stable API error envelope

All handled application errors use:

```json
{
  "error": {
    "code": "RUN_NOT_FOUND",
    "message": "run ... does not exist",
    "details": {}
  }
}
```

`details` is always an object and may contain machine-readable workflow information such as:

```json
{
  "run_id": "...",
  "stage": "EXAMINE",
  "attempt": 2,
  "operation_scope": "rule-evaluation-v1",
  "claim_generation": 3
}
```

### HTTP category mapping

| HTTP | Category | Examples |
|---|---|---|
| 400 | semantically invalid command | invalid idempotency-key format, invalid paired rule selection |
| 404 | durable resource absent | pile/run/finding/report not found |
| 409 | durable state conflict | not reviewable, stale review/change set, operation in progress, already resolved, previously failed, escalated/awaiting-upstream state |
| 413 | upload exceeds source-size limit | source too large |
| 415 | unsupported source type/feature | unsupported parser/document feature |
| 422 | FastAPI transport/schema validation | malformed JSON/form/type |
| 503 | transient infrastructure/publication failure | database unavailable, artifact/source publication transient failure |
| 500 | unclassified integrity/programming defect | never convert integrity corruption into a false success |

Typed integrity errors may initially map to 500 with a stable error code and no sensitive internals; they are not 409 merely because the client happened to trigger discovery of corruption.

## 7. MCP tool contract (SDK v2)

Phase 6B-4 implements these locked tool names with the official MCP Python SDK v2. The MCP server calls the same application/use-case services as REST and does not call REST over HTTP.

| MCP tool | REST-equivalent capability |
|---|---|
| `create_pile` | POST pile |
| `create_rule_set` | POST rule set |
| `ingest_document` | POST document ingestion |
| `get_pile` | GET pile |
| `list_pile_runs` | GET pile runs |
| `get_run` | GET run/stages |
| `advance_run` | POST advance |
| `get_run_facts` | GET facts |
| `get_run_findings` | GET findings |
| `get_run_review` | GET review |
| `review_finding` | POST finding decision |
| `review_update` | POST UPDATE decision |
| `get_run_report` | GET report metadata/content |
| `get_run_metrics` | GET metrics |

MCP `ingest_document` accepts `pile_id`, `logical_key`, `filename`, `media_type`, and canonical `source_base64`. Base64 is used because MCP tool arguments are JSON-shaped; the decoded bytes are passed unchanged to the same `IngestAndReconcileDocumentService` used by REST. `get_run_report` returns verified final-output metadata plus the exact committed UTF-8 Markdown content in one structured result.

The default server transport is stdio. Set `TASK1_MCP_TRANSPORT=streamable-http` (or `sse`) to select another SDK transport. The installed console entry point is `task1-mcp`. There is intentionally no `commit_run` tool: a machine must call `advance_run`, which preserves the human-review gate and enters COMMIT only when review is complete and approved.

## 8. Application-layer facades implemented from the interface audit

Phase 6A intentionally optimized command-side runtime semantics. The 6B interface audit identified a small set of application/query facades that were then implemented outside transport handlers.

### 8.1 Implemented in 6B-2

1. `CreatePileService`
   - validates pile creation input;
   - performs repository transaction;
   - returns durable pile record.

2. `CreateRuleSetService`
   - creates a rule-set version and its rules atomically;
   - does not alter captured Run rule scope.

3. `IngestAndReconcileDocumentService`
   - composes existing ingestion and arrival reconciliation;
   - returns document/version plus created/reconciled Run IDs;
   - exact replay remains provider/parser free.

4. Read/query application services (or one narrowly grouped query facade) for:
   - pile detail/list documents/list runs;
   - document/version history;
   - Run + RunStep attempts;
   - run-local Facts/Findings/evidence;
   - RunMetrics;
   - final Output metadata/content.

These services are read projections only; they do not become alternate state-transition paths.

### 8.2 Implemented in 6B-3

5. `ExternalReviewQueryService`
   - produces stable review and final-report projections from durable Run/Finding/evidence/change-set/output/audit state;
   - computes a read-only UPDATE change-set preview only after finding review is complete;
   - verifies committed artifact bytes before returning report content;
   - performs no approval or finalization writes.

6. `ExternalUpdateReviewService`
   - materializes/replays the durable UPDATE change set through `BuildUpdateChangeSetService`;
   - requires the caller to echo the reviewed change-set fingerprint;
   - rejects stale previews before any UPDATE approval/rejection decision is recorded;
   - delegates the terminal decision to `ReviewUpdateChangeSetService`.

7. `ExternalAdvanceRunService`
   - delegates every actual stage transition to `RunWorkflowService`;
   - when a Run is already `REVIEW_REQUIRED`, refuses to start COMMIT unless the durable external review projection says `commit_ready=true`;
   - preserves the deeper COMMIT/finalizer review checks as defense in depth.

The existing command services remain authoritative for:

- `ReviewDecisionService`;
- `ReviewUpdateChangeSetService`;
- `RunWorkflowService`;
- `CommitFinalReportService` (called through workflow for external flow).

## 9. Phase 6B implementation record

### 6B-1 — contract and skeleton

Core contract files:

```text
src/api/rest/app.py                         # include versioned router + handlers registration
src/api/rest/router.py                      # `/api/v1` root router
src/api/rest/schemas/common.py              # stable error/common DTOs
src/api/rest/errors.py                      # typed error envelope utilities
src/api/interface_contract.py               # transport-neutral operation names/manifest
src/api/mcp/contracts.py                    # locked MCP tool-name manifest
src/api/mcp/server.py                       # MCPServer registration + stdio/HTTP runner
src/api/mcp/service_provider.py             # lazy shared application-service wiring
tests/unit/test_rest_contract.py
tests/unit/test_interface_contract.py
```

No business endpoint should be a fake 501 route merely to make OpenAPI look complete. Only implemented routes are exposed. The contract document/manifest locks planned operations; 6B-2 and 6B-3 register them as they become real.

### 6B-2 — core REST

Implemented core application facades plus Pile/Rule/Document/Run read and advance REST endpoints.

### 6B-3 — review and COMMIT REST

Implemented review projection, durable review decisions, guarded workflow advancement, and verified final-report REST endpoints.

### 6B-4 — MCP

Implemented the MCP server using the same application facades/services, with structured tool schemas, exact base64 source ingestion, and no direct COMMIT bypass.

### 6B-5 — React review UI

Implemented the React + TypeScript human-review UI over REST.

### 6B-6 — black-box E2E

Implemented isolated PostgreSQL REST/MCP end-to-end workflows plus a Playwright browser workflow using test-only deterministic semantic providers.

### 6B-7 — closure

Final documentation, reproducible verification, handoff, and checkpoint commit preparation.

## 10. 6B-1 acceptance criteria

The skeleton checkpoint is complete when:

1. `/health` behavior is unchanged;
2. versioned router infrastructure exists at `/api/v1` without fake product routes;
3. common API error envelope is tested;
4. interface operation manifest has unique REST method/path and MCP tool identities;
5. REST and MCP manifests point to the same conceptual operations;
6. no REST/MCP module imports repository implementations directly;
7. no domain module imports FastAPI/MCP;
8. full Phase-6A tests remain green;
9. Ruff, pip check and `git diff --check` pass.

## 11. Decisions deliberately deferred

- Authentication/authorization: not required for the take-home behavior; explicit durable actor fields remain part of review commands. Add auth only if needed for deployment polish.
- Watcher daemon: 6C.
- Redis/background worker: 6C unless real 6B integration pressure proves it necessary sooner.
- LangGraph migration: not required merely to satisfy the orchestration-framework wording because the Engineer brief explicitly allows a hand-rolled agent loop; do not destabilize the proven PostgreSQL workflow without a concrete reason.
- React implementation details/design system: 6B-5.
- MCP binary-content encoding: finalized in 6B-4 as canonical base64 in JSON tool arguments; decoded bytes are passed unchanged to the shared ingestion facade.

## 12. Phase 6B closure invariants

The implemented external surfaces must preserve these properties through final verification:

1. REST and MCP call shared application services; neither transport contains repository SQL or an alternate workflow implementation.
2. The locked MCP contract contains exactly 14 tools and no `commit_run` bypass.
3. REST exposes no direct `/commit` writer; fresh finalization is reached only by advancing the durable `COMMIT` RunStep after review.
4. Finding review remains item-level and mixed APPROVE/REJECT decisions are supported in one Run.
5. UPDATE approval is bound to the exact reviewed change-set fingerprint.
6. Final report reads verify durable Output/audit proof and artifact bytes before returning content.
7. Browser state is projection/UI state only; refreshing from the API reconstructs authoritative progress.
8. Production semantic execution still requires configured Groq providers. Deterministic E2E providers live only under `tests/e2e/`.
9. Phase 6B adds no schema migration and does not weaken Phase 6A concurrency, provenance, retry, or focused-update guarantees.
10. Watcher/queue/background-worker operationalization remains deferred; the PostgreSQL-authoritative hand-rolled workflow remains the production orchestration authority.


## 13. Post-6B provider checkpoint: Groq

The external REST/MCP/UI contract is unchanged, but the default production semantic provider is Groq. `GROQ_API_KEY` is the credential required by the external production wiring; the three semantic stages share one rate-limited Groq transport and default to `openai/gpt-oss-20b`.

The transport disables SDK-level retries and implements explicit pacing/backoff so one workflow cannot accidentally burst the free-tier quota. It also consumes Groq rate-limit response headers, keeps a configurable token reserve, honors `retry-after`, uses low reasoning effort, and caps completion tokens. Existing workflow idempotency remains the first line of defense against duplicate semantic calls.

Document ingestion uses the local deterministic embedding provider and therefore does not consume Groq requests or tokens.
