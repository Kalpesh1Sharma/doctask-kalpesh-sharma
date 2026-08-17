# Task 1 React review UI

The Phase 6B React + TypeScript UI is a thin human-review client for the shared REST/application layer. It does not own workflow or approval truth; PostgreSQL-backed application services remain authoritative.

## Local development

Start the backend from the repository root:

```powershell
uvicorn api.rest.app:app --reload
```

Then in another terminal:

```powershell
cd frontend
npm install
npm run dev
```

Vite serves the UI on `http://127.0.0.1:5173` and proxies `/api/*` and `/health` to FastAPI on port 8000.

## Unit/type/build verification

```powershell
npm test
npm run check
npm run build
```

Vitest only collects `src/**/*.test.{ts,tsx}`. Playwright scenarios are intentionally separate.

## Browser E2E

Install Chromium once after `npm install`:

```powershell
npx playwright install chromium
```

Then run:

```powershell
npm run test:e2e
```

Playwright starts an isolated E2E FastAPI process and Vite automatically. The backend harness creates a disposable PostgreSQL database and injects deterministic semantic providers strictly for test execution, so this workflow does not require a live OpenAI key.

The browser scenario proves source upload, visible durable stages, evidence review, human approval, COMMIT, and verified report retrieval.

## Covered UI capabilities

- pile creation/opening;
- source ingestion and immutable document-version history;
- rule-set creation and scope selection;
- five-stage Run/attempt status;
- Facts/Findings with exact evidence;
- item-level approve/reject review;
- fingerprint-bound UPDATE review;
- COMMIT readiness and advancement through the shared REST API;
- Run metrics;
- verified final report and focused-update proof.
