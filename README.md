# Task 1 — Document Analysis, Review, and Focused Update System

This repository implements the Task 1 take-home as a PostgreSQL-authoritative modular monolith with:

- immutable document/version ingestion with exact replay and concurrency fencing;
- grounded Fact/Finding provenance down to evidence spans;
- a five-stage durable workflow: `INGEST -> UNDERSTAND -> EXAMINE -> REVIEW -> COMMIT`;
- an explicit human approval gate;
- deterministic final reports and byte-proven focused UPDATEs;
- provider/model/token/cost telemetry;
- a versioned FastAPI REST interface;
- an MCP machine interface using the same application services;
- a React + TypeScript review UI;
- PostgreSQL, REST, MCP, and Playwright end-to-end regression coverage;
- watched-folder ingestion plus a background Run worker for continuous operation.

PostgreSQL is the authoritative durable state store. REST, MCP, and the browser are adapters over the same application layer; none of them owns workflow truth.

## Prerequisites

- Python 3.12–3.14
- Node.js 20.19+
- Docker / Docker Compose
- PostgreSQL client access through the supplied Docker container
- a Groq API key for the production semantic workflow

The deterministic providers under `tests/e2e/` are test-only and do not replace the production Groq configuration.

## Quick start — one operational command

The default Docker Compose stack is the shortest path from a fresh clone to the running product. If you want real semantic execution, export `GROQ_API_KEY` in your shell (or place it in a local `.env`) before startup. The stack itself boots without a live key so health checks and key-free verification remain available.

From the repository root:

```powershell
docker compose up --build
```

That single command starts PostgreSQL/pgvector, applies the ordered `001 -> 008` migration chain, starts FastAPI, the background Run worker, the watch-folder daemon, and the built React UI behind nginx. Open:

```text
UI:  http://127.0.0.1:5173
API: http://127.0.0.1:8000
```

Compose waits for PostgreSQL health, requires the migration job to complete successfully before any application process starts, and requires API health before the frontend starts. Migrations are replay-safe at the deployment layer: `docker/migrate.sh` records filename/checksum receipts in the separate `task1_meta` PostgreSQL schema and adopts an already-applied manual schema when its expected marker is present. This operational metadata does **not** add a public/domain table; migration `008` remains latest and the application schema remains 31 public tables.

The API, worker, and watcher share named source/artifact volumes. The host directory `var/watch/` is mounted at `/data/watch` in the application containers. For a Docker-operated watched Pile, configure a path such as `/data/watch/mortgage-file`, then place files under `var/watch/mortgage-file/` on the host.

Useful operational commands:

```powershell
# detached startup
docker compose up -d --build

# logs
docker compose logs -f api worker watcher

# run the stdio MCP server in the same application image
docker compose run --rm mcp

# stop while preserving PostgreSQL/source/artifact volumes
docker compose down

# destructive clean reset
docker compose down -v
```

The black-box startup/restart/migration smoke check is:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\phase6c4_operational_verify.ps1
```

It builds the stack, waits for API/UI health, verifies all runtime daemons are running, checks the 31-table schema and eight migration receipts, re-runs migration startup to prove idempotency, and restarts the worker/watcher.

## 1. Backend setup

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Create the local environment file:

```powershell
Copy-Item .env.example .env
```

The default `.env.example` matches the supplied Docker Compose PostgreSQL service. Set `GROQ_API_KEY` before advancing production runs through the semantic stages.


### Groq free-tier pacing

The default semantic model is `openai/gpt-oss-20b` served by Groq. The backend deliberately disables the SDK's hidden retry loop and applies its own shared rate limiter across fact extraction, contradiction detection, and rule evaluation. Defaults cap process-local traffic at 20 requests/minute, space requests by at least 3.1 seconds, use low reasoning effort with a 1024-token completion cap, honor `retry-after` on HTTP 429, and consume Groq's remaining-token/reset headers before issuing the next semantic call. Exact durable idempotency also prevents successful provider calls from being repeated on replay.

Groq account limits can differ, so the current limits page in the Groq console remains authoritative. If your account has lower limits, reduce `GROQ_MAX_REQUESTS_PER_MINUTE` or increase `GROQ_MIN_REQUEST_INTERVAL_SECONDS`. Document ingestion embeddings remain local and consume no Groq quota. Each newly READY DocumentVersion is also classified once into a controlled mortgage-document role through the shared Groq structured-output transport; exact classification replay does not call Groq again.

For backend-only development, start PostgreSQL without the rest of the Compose stack and run the same migration job used by the operational startup:

```powershell
docker compose up -d postgres
docker compose run --rm migrate
```

The current executable chain is `001 -> 008` and produces 31 public tables. The migration job is safe to invoke again; it verifies recorded checksums and skips already-applied migrations.

## 2. REST API

Start FastAPI from the repository root:

```powershell
uvicorn api.rest.app:app --reload
```

Useful entry points:

```text
GET  /health
POST /api/v1/piles
POST /api/v1/piles/{pile_id}/rule-sets
POST /api/v1/piles/{pile_id}/documents
GET  /api/v1/runs/{run_id}
POST /api/v1/runs/{run_id}/advance
GET  /api/v1/runs/{run_id}/review
POST /api/v1/runs/{run_id}/findings/{finding_id}/decision
POST /api/v1/runs/{run_id}/update/decision
GET  /api/v1/runs/{run_id}/report
GET  /api/v1/runs/{run_id}/report/content
GET  /api/v1/runs/{run_id}/metrics
```

There is intentionally no direct REST `commit` endpoint. A client advances the durable workflow, completes human review, then advances again into `COMMIT`.

For UPDATE Runs, `GET /review` exposes material Fact changes as `update_change_set.items`. Call `POST /update/decision` once per item with its `fact_id`; APPROVED and REJECTED decisions may be mixed in the same review. UPDATEs with no material Fact items keep the legacy whole-change-set approval call without `fact_id`.

The complete interface contract is in `docs/PHASE_6B_INTERFACE_CONTRACT.md`.

## 3. MCP machine interface

The installed console entry point is:

```powershell
task1-mcp
```

The default transport is stdio. For another SDK transport, set the shell environment variable before starting the process, for example:

```powershell
$env:TASK1_MCP_TRANSPORT = "streamable-http"
task1-mcp
```

The MCP server exposes the original 14 workflow operations plus two read-only 6C-3 change-history tools (`list_pile_change_history` and `get_run_change_history`) and deliberately has no `commit_run` bypass tool. `advance_run` preserves the same human-review gate as REST.

## 4. React review UI

Install the frontend dependencies once:

```powershell
cd frontend
npm install
```

For local development, keep FastAPI running on port 8000 and start Vite:

```powershell
npm run dev
```

Open `http://127.0.0.1:5173`.

The UI supports pile/rule/document setup, immutable version history, five-stage Run status, exact evidence review, mixed Finding approve/reject decisions, fingerprint-bound UPDATE review, COMMIT readiness, metrics, verified report retrieval, and focused-update proof display.


## 5. Watched-folder ingestion

A Pile can opt into continuous filesystem ingestion by setting `watch_enabled=true` and a `watch_location` when it is created. The watcher waits for a source to remain unchanged before ingesting it, derives the logical document key from its path relative to the watch root, and supports `.pdf`, `.docx`, and `.txt`.

Start the watcher from the repository root:

```powershell
task1-watch
```

The watcher reuses the same durable ingestion, semantic classification, and arrival-reconciliation application services as REST/MCP. Duplicate filesystem events are suppressed in-process, and a process restart safely exact-replays already completed ingestion/classification instead of creating duplicate history.

Example watch-enabled Pile request for local Python execution:

```json
{
  "name": "Mortgage file",
  "watch_enabled": true,
  "watch_location": "var/watch/mortgage-file"
}
```

When the Compose stack is running, use the container-visible path instead:

```json
{
  "name": "Mortgage file",
  "watch_enabled": true,
  "watch_location": "/data/watch/mortgage-file"
}
```

A file such as `var/watch/mortgage-file/closing/disclosure.pdf` is ingested with logical key `closing/disclosure`. Supported semantic roles are `LOAN_ESTIMATE`, `CLOSING_DISCLOSURE`, `PROMISSORY_NOTE`, `SETTLEMENT_STATEMENT`, `UNDERWRITING_APPROVAL`, and conservative `UNKNOWN`. Source text is treated as untrusted data during classification.

## 6. Background Run worker

The operational worker discovers non-terminal automatic `INITIAL`/`UPDATE` Runs and advances them through the same durable workflow used by REST/MCP. Automatic watched Runs have no interactive caller at the first scope-capture step, so the worker deterministically selects the latest version of each RuleSet name in the Run's Pile; a Pile with no RuleSets gets an explicitly captured empty rule scope. Explicit REST/MCP advancement keeps the existing caller-supplied scope behavior.

Start it from the repository root:

```powershell
task1-worker
```

The worker may drive `PENDING/RUNNING` through `INGEST -> UNDERSTAND -> EXAMINE -> REVIEW`, but it **always yields when the Run first reaches `REVIEW_REQUIRED`**. On later polls it calls the guarded external advance service: incomplete human review remains waiting, while a review-complete Run may enter `COMMIT`. The worker never writes Finding or UPDATE review decisions itself. Blocked UPDATE children are resumed only after their durable parent is `COMPLETED`.

Multiple worker processes are safe to run. Before driving a candidate, each process takes a **session-scoped PostgreSQL advisory claim keyed by Run id**. Only one worker process can hold that transient claim at a time; the durable `runs` / `run_steps` / idempotency records remain authoritative. If a worker process or PostgreSQL session dies, PostgreSQL releases the advisory claim automatically and another worker can resume the Run from durable workflow state. Both the watcher and worker retry their polling loop after transient scan/database failures instead of exiting, and their CLI entry points explicitly open/close the shared database pool. No Redis queue or second durable lock store is introduced.

Runtime pacing is configurable with `WORKER_POLL_INTERVAL_SECONDS`, `WORKER_BATCH_SIZE`, and `WORKER_MAX_ADVANCES_PER_RUN`. Run the watcher and worker in separate terminals for continuous local operation.

## 7. Durable change-history query model

Committed UPDATE history is reconstructed from the immutable rows that already authorized the commit: `runs`, `update_change_sets`, per-Fact `update_change_items`, human review decisions, exact Fact evidence, source document versions, the final output row, and the COMMIT audit receipt. There is intentionally **no duplicate history table and no Migration 009** in this checkpoint; migration 008 remains the latest executable schema migration.

The projection represented by `ChangeHistoryService` records, for every completed UPDATE:

- **what was proposed** — each material Fact creation/removal;
- **what actually changed** — `APPLIED` only when that item's human decision was `APPROVED`;
- **what was rejected** — rejected proposals remain visible for audit but are not reported as committed changes;
- **when it committed** — the durable Run completion timestamp;
- **because of which source** — the trigger document/version, logical key, content hash, and optional semantic role;
- **where the claim came from** — exact immutable evidence spans and their source versions;
- **who gated it** — the item-level (or zero-item whole-update) review actor and decision; and
- **which artifact/run committed it** — Run, change-set, output id/hash, plus the COMMIT actor when a Phase-6 audit receipt exists.

The query fails closed if a completed UPDATE cannot prove its change set, output, lineage, required review decisions, Fact, or exact evidence. Historical pre-cutover outputs may truthfully report an unknown COMMIT actor rather than invent one. REST exposes `GET /api/v1/piles/{pile_id}/change-history` and `GET /api/v1/runs/{run_id}/change-history`; MCP exposes the matching `list_pile_change_history` and `get_run_change_history` tools. Both are thin adapters over the same `ChangeHistoryService`.

## 8. Verification

Backend/static checks:

```powershell
pytest -q
ruff check .
python -m pip check
git diff --check
```

Frontend checks:

```powershell
cd frontend
npm test
npm run check
npm run build
npm run test:e2e
```

The Playwright E2E suite starts its own FastAPI/Vite processes and uses an isolated PostgreSQL database. You do not need to manually start `uvicorn`, Vite, or `task1-mcp` for that test.

For the Phase 6B closure run, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\phase6b_final_verify.ps1
```

For the Phase 6C-5 operational behavioral proof, use:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\phase6c5_operational_e2e_verify.ps1
```

That suite deliberately clears `GROQ_API_KEY` and uses deterministic semantic providers, while still exercising real PostgreSQL/pgvector state, watched-folder ingestion, semantic classification, automatic worker advancement, the human review gate, mixed item-level UPDATE decisions, focused COMMIT, REST/MCP change history, concurrency fencing, and the browser E2E. Its hostile source contains explicit instructions to auto-approve, skip review, commit, and invent an unsupported value; the run must still stop at `REVIEW_REQUIRED`, persist only grounded evidence-backed facts, and commit only after explicit external review decisions.

## 9. Source and artifact storage

Defaults:

```text
source bytes:   var/sources
final artifacts: var/artifacts
```

Both paths are configurable through environment settings. Published content is content-addressed and verified by hash before authoritative replay/read paths trust it.

## 10. Design references

- `SCHEMA_DESIGN.md` — relational model and migration invariants
- `docs/PHASE_6B_INTERFACE_CONTRACT.md` — REST/MCP external contract
- `.codex-context/ARCHITECTURE_DESIGN.md` — architecture record
- `.codex-context/PROJECT_HANDOFF.md` — local continuation checkpoint

The executable migrations and current tests are the source of truth for implemented database/runtime behavior.

### pgvector analysis retrieval

UNDERSTAND now uses PostgreSQL/pgvector cosine retrieval to select the bounded source-chunk context sent to the fact-extraction provider. Chunk vectors and query vectors use the same local deterministic embedding provider, so retrieval does not consume Groq quota. Retrieved chunks remain immutable source records and the existing evidence validator still rejects citations outside that selected scope. Configure `ANALYSIS_RETRIEVAL_TOP_K` and `ANALYSIS_RETRIEVAL_QUERY` in the backend environment.
