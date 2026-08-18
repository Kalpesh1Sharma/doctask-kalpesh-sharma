# Architecture Design — Final Task 1 Implementation

## 1. Scope

This document records the architecture actually implemented for Task 1 at the end of Phase 6C. It describes current behavior, not an aspirational target.

Source-of-truth order:

1. Engineer task specification — product requirements.
2. Executable migrations `sql/migrations/001_...` through `008_...` — database contract.
3. Current application/tests — implemented runtime behavior.
4. This document — architecture and trade-off explanation.

## 2. System shape

The system is a PostgreSQL-authoritative modular monolith with separate runtime processes over one shared application/domain core.

```text
                           Browser
                             |
                             v
                    React + TypeScript UI
                             |
                             v
                         FastAPI REST
                             |
            +----------------+----------------+
            |                                 |
            v                                 v
     Application services              MCP interface
            |
            v
   Durable workflow / use cases
            |
      +-----+--------------------------+
      |                                |
      v                                v
 Domain rules                    Repositories
                                      |
                                      v
                            PostgreSQL + pgvector

 Watch-folder daemon ------> same application services
 Background Run worker ----> same durable workflow

 Source/artifact bytes ----> content-addressed filesystem storage
 Semantic providers -------> Groq structured-output API
 Retrieval embeddings -----> local deterministic embedding provider
```

REST, MCP, the watcher, the worker, and the browser do not own separate business state. They all drive the same application services and PostgreSQL-backed workflow.

## 3. Durable workflow orchestration

The implemented workflow is a hand-rolled durable agent loop centered on `RunWorkflowService` and PostgreSQL state. The five durable stages are:

```text
INGEST -> UNDERSTAND -> EXAMINE -> REVIEW -> COMMIT
```

This project does **not** use LangGraph in production. That is a deliberate implementation choice, not an omitted dependency. The task permits a hand-rolled agent loop as a comparable orchestration approach.

The workflow is agentic because stage outcomes can change execution: operations can retry, wait on upstream state, escalate, stop for human review, or resume after a restart. Durable `runs`, `run_steps`, idempotency records, review rows, audit events, and outputs are authoritative; an in-memory graph is not required for recovery.

## 4. PostgreSQL is authoritative

PostgreSQL is the single durable source of truth for:

- Piles, logical Documents, and immutable DocumentVersions;
- source chunks and pgvector embeddings;
- exact EvidenceSpans;
- Facts and Findings plus run membership;
- durable Run and RunStep history;
- captured RuleSet/Rule scope;
- UPDATE change sets and item-level review decisions;
- human Finding decisions;
- final Outputs and focused-update proof;
- idempotency claims/generations;
- provider usage and per-stage metrics;
- document-role classification;
- audit/change-history reconstruction.

There is no Redis database, queue, or second durable lock store.

## 5. Concurrency and restart model

Authoritative concurrency uses PostgreSQL constraints, row locks, idempotency generations, and transaction boundaries.

The operational background worker additionally uses a **session-scoped PostgreSQL advisory lock keyed by Run id** as transient scheduling coordination. This lock is not business state. If the process, session, or database connection dies, PostgreSQL releases it and another worker can rediscover the Run from durable state.

The watcher and worker are polling daemons. They retry transient scan/database failures rather than exiting. A process restart therefore re-enters from PostgreSQL and content-addressed source state instead of starting a new logical run.

## 6. Document ingestion and classification

The accepted source formats are deliberately bounded:

- `.txt`;
- text-bearing PDF;
- a restricted DOCX subset.

Scanned/image-only PDFs are rejected because OCR is not part of this implementation. Complex DOCX constructs that the parser cannot prove safe/grounded are rejected explicitly rather than silently dropped.

Ingestion is content-addressed and fenced for concurrency. A successful READY version is immutable. Exact repeated content replays without repeating parser/embedding work.

Each newly READY DocumentVersion is semantically classified into one controlled mortgage-document role:

- `LOAN_ESTIMATE`;
- `CLOSING_DISCLOSURE`;
- `PROMISSORY_NOTE`;
- `SETTLEMENT_STATEMENT`;
- `UNDERWRITING_APPROVAL`;
- `UNKNOWN`.

Known roles require grounded source-chunk evidence. `UNKNOWN` is preferred over guessing.

## 7. Retrieval and grounding

Document chunks store pgvector embeddings in PostgreSQL. The production UNDERSTAND path performs bounded cosine retrieval before fact extraction.

Retrieval embeddings are produced locally by the deterministic embedding provider. This avoids using Groq quota for embeddings and guarantees that stored document vectors and query vectors use the same representation.

Retrieval is only a context-selection mechanism. It does not create provenance. A Fact or Finding is accepted only through the existing exact evidence-validation path, so model output cannot cite a chunk outside the authorized source scope.

## 8. Semantic provider boundary

Production semantic structured-output calls use Groq for:

- document-role classification;
- fact extraction;
- contradiction detection;
- rule evaluation.

The default model is configured through environment settings and currently defaults to `openai/gpt-oss-20b` on Groq.

Provider access is isolated behind typed provider/transport adapters. Durable workflow/domain code is not coupled to Groq response objects.

Rate control is application-owned:

- SDK hidden retries are disabled;
- a shared process-local rate limiter paces semantic calls;
- HTTP 429 `retry-after` is honored;
- remaining-token/reset headers are consumed when available;
- successful durable operations are not paid for again on exact replay.

Tests do not require a live Groq key. Deterministic providers are injected at the provider boundary while real PostgreSQL, pgvector, workflow, REST, MCP, and browser paths remain exercised.

## 9. Human review is a hard authority boundary

A model/provider cannot approve or commit its own work.

Findings are reviewed item by item. UPDATE material Fact changes are also reviewed item by item, allowing APPROVED and REJECTED decisions in the same review. The final focused update uses only approved material changes.

`COMMIT` is the only fresh finalization path. REST and MCP expose guarded workflow advancement rather than a direct commit bypass.

Hostile instructions embedded in a source document remain untrusted source data. They are serialized into provider inputs but have no authority over Run status, review state, or COMMIT.

## 10. Focused UPDATE model

UPDATE runs start from a committed baseline and a trigger DocumentVersion.

The system computes a fingerprint-bound change set, records item-level human decisions, and produces a deterministic focused patch against the baseline final artifact. Rejected proposed Fact changes stay in history but do not become effective committed state.

The COMMIT audit proof records baseline identity/hash and preserved byte-range hashes so unchanged baseline regions are provably copied unchanged where applicable.

This is intentionally different from performing a full semantic rerun and merely obtaining similar output bytes.

## 11. Change history

`ChangeHistoryService` reconstructs committed UPDATE history from immutable authoritative rows rather than a duplicate history table.

It answers:

- what was proposed;
- what actually changed;
- what was rejected;
- when the update committed;
- which trigger source caused it;
- which evidence supported the changed claim;
- who reviewed it;
- which output/run committed it.

The same projection is exposed through REST and MCP.

## 12. External interfaces

### REST

FastAPI routes are thin transport adapters over application services. They perform request/response validation and error mapping but do not implement workflow policy or repository SQL directly.

### MCP

MCP exposes machine-driveable equivalents over the same application services. It includes explicit review operations and read-only change-history operations. It does not call REST internally and has no `commit_run` bypass tool.

### React review UI

The React/TypeScript UI drives REST and displays durable workflow stages, exact evidence, Finding decisions, UPDATE item decisions, COMMIT readiness, metrics, reports, and focused-update proof.

## 13. Storage

Original source bytes and final artifacts are stored outside relational rows in content-addressed filesystem storage. PostgreSQL stores authoritative metadata, hashes, lineage, and provenance.

Reads re-verify expected URI/hash/actual bytes before trusting an artifact. Source-address collisions with incorrect bytes fail integrity rather than overwriting immutable content.

Under Docker Compose, source/artifact directories are shared named volumes and the external watch directory is a host bind mount at `/data/watch`.

## 14. Deployment topology

The documented one-command runtime is:

```powershell
docker compose up --build
```

Compose starts:

```text
PostgreSQL/pgvector
        |
        v
migration job (001 -> 008)
        |
        +--> FastAPI API
        +--> background worker
        +--> watch-folder daemon
        +--> React/nginx frontend (after API health)
```

The deployment migration runner stores filename/checksum receipts in a separate `task1_meta` schema. This is deployment metadata, not domain state; the public application schema remains 31 tables and Migration 008 remains the latest domain migration.

The stdio MCP server is launched on demand from the same application image rather than kept as an idle daemon.

## 15. Metrics and cost

Every closed workflow attempt has a durable RunMetric. Provider-backed attempts also have generation-scoped provider usage events.

When the provider supplies usage, model and token data are persisted. Estimated cost is calculated only when explicit model pricing is configured; missing pricing remains `NULL` rather than inventing a cost.

This preserves stage/attempt timing and cost history across retries and escalations.

## 16. Important trade-offs

### Hand-rolled workflow instead of LangGraph

**Buys:** one authoritative recovery model, direct alignment with the relational Run/RunStep state machine, easier fail-closed replay checks, and less duplicate checkpoint state.

**Costs:** more workflow/retry code is owned by this repository and future graph-shaped branching requires explicit implementation.

### PostgreSQL advisory coordination instead of Redis/queue infrastructure

**Buys:** no second state/lock system, simpler fresh-clone operation, automatic claim release on session death.

**Costs:** the current worker is polling rather than event-driven and PostgreSQL carries transient scheduling coordination in addition to durable state.

### Local deterministic embeddings instead of a hosted embedding API

**Buys:** no embedding quota/cost, reproducible offline tests, one embedding representation for documents and queries.

**Costs:** retrieval quality is intentionally modest compared with a production-grade learned embedding model.

### Restricted parser support

**Buys:** unsupported structures fail explicitly instead of producing silently incomplete evidence.

**Costs:** scanned PDFs/OCR and complex DOCX documents are outside the accepted format subset.

## 17. Security and honesty boundaries

- API keys are environment-only and must never be committed or logged.
- Source-document instructions are untrusted data, not control messages.
- Unknown/unsupported information is surfaced rather than invented.
- A success response is emitted only after durable state proves the claimed result.
- Tests exercise the critical behaviors without requiring a live model key.

## 18. Final architectural invariants

Preserve these unless a future requirement deliberately changes them:

1. PostgreSQL is the only authoritative durable business-state store.
2. REST, MCP, watcher, worker, and browser share application/use-case logic.
3. Domain code remains infrastructure-independent.
4. The durable five-stage workflow is `INGEST -> UNDERSTAND -> EXAMINE -> REVIEW -> COMMIT`.
5. Human review is mandatory before fresh commit.
6. Exact evidence provenance is required for persisted claims.
7. UPDATEs are focused and rejected items do not enter committed effective state.
8. Durable idempotency/generation fencing protects expensive/replayed work.
9. PostgreSQL row locks/constraints protect authoritative concurrency; advisory locks are transient worker scheduling only.
10. There is no Redis, LangGraph runtime, or second authoritative checkpoint store in the implemented system.
