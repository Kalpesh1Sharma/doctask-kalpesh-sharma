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
- PostgreSQL, REST, MCP, and Playwright end-to-end regression coverage.

PostgreSQL is the authoritative durable state store. REST, MCP, and the browser are adapters over the same application layer; none of them owns workflow truth.

## Prerequisites

- Python 3.12–3.14
- Node.js 20.19+
- Docker / Docker Compose
- PostgreSQL client access through the supplied Docker container
- a Groq API key for the production semantic workflow

The deterministic providers under `tests/e2e/` are test-only and do not replace the production Groq configuration.

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

Groq account limits can differ, so the current limits page in the Groq console remains authoritative. If your account has lower limits, reduce `GROQ_MAX_REQUESTS_PER_MINUTE` or increase `GROQ_MIN_REQUEST_INTERVAL_SECONDS`. Document ingestion itself uses the local deterministic embedding provider and therefore consumes no Groq quota.

Start PostgreSQL:

```powershell
docker compose up -d
```

Apply the ordered migrations once to a fresh database:

```powershell
Get-ChildItem .\sql\migrations\*.sql |
  Sort-Object Name |
  ForEach-Object {
    Get-Content -Raw $_.FullName |
      docker exec -i task1-postgres psql -v ON_ERROR_STOP=1 -U task1 -d task1
    if ($LASTEXITCODE -ne 0) { throw "Migration failed: $($_.Name)" }
  }
```

The current executable chain is `001 -> 006` and produces 27 public tables.

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

The MCP server exposes the same 14 external operations as the REST/application surface and deliberately has no `commit_run` bypass tool. `advance_run` preserves the same human-review gate as REST.

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

## 5. Verification

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

## 6. Source and artifact storage

Defaults:

```text
source bytes:   var/sources
final artifacts: var/artifacts
```

Both paths are configurable through environment settings. Published content is content-addressed and verified by hash before authoritative replay/read paths trust it.

## 7. Design references

- `SCHEMA_DESIGN.md` — relational model and migration invariants
- `docs/PHASE_6B_INTERFACE_CONTRACT.md` — REST/MCP external contract
- `.codex-context/ARCHITECTURE_DESIGN.md` — architecture record
- `.codex-context/PROJECT_HANDOFF.md` — local continuation checkpoint

The executable migrations and current tests are the source of truth for implemented database/runtime behavior.
