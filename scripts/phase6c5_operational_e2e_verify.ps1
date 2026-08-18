param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$originalGroqApiKey = $env:GROQ_API_KEY

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )
    Write-Host "[6C-5] $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

try {
    # The behavioral proof must not depend on a paid/live provider key. The
    # operational E2E harness wires deterministic semantic providers while still
    # exercising real PostgreSQL, pgvector, filesystem watch ingestion, worker
    # orchestration, REST, MCP, review, COMMIT, and change-history code paths.
    $env:GROQ_API_KEY = ""

    Invoke-Checked "hostile watched-document operational E2E" {
        pytest -q tests/e2e/test_operational_runtime.py
    }

    Invoke-Checked "real pgvector provider-scope integration" {
        pytest -q tests/application/test_extract_run_facts_service.py -k "pgvector_retrieval"
    }

    Invoke-Checked "worker concurrency and restart claims" {
        pytest -q tests/application/test_run_worker_claims.py
    }

    Invoke-Checked "watcher and worker restart replay" {
        pytest -q tests/unit/test_watch_folder.py -k "process_restart"
        if ($LASTEXITCODE -ne 0) { throw "watcher restart replay failed" }
        pytest -q tests/unit/test_run_worker.py -k "restart_resumes"
    }

    Invoke-Checked "concurrent document-ingestion fencing" {
        pytest -q tests/application/test_document_runtime_services.py -k "concurrent"
    }

    Invoke-Checked "provider trust-boundary prompt-injection regression" {
        pytest -q tests/unit/test_groq_structured_transport.py -k "keeps_untrusted_payload_separate"
    }

    Invoke-Checked "existing REST/MCP black-box E2E" {
        pytest -q tests/e2e/test_rest_workflow.py tests/e2e/test_mcp_workflow.py
    }

    if (-not $SkipFrontend) {
        Push-Location frontend
        try {
            Invoke-Checked "frontend unit tests" { npm test }
            Invoke-Checked "frontend type check" { npm run check }
            Invoke-Checked "frontend production build" { npm run build }
            Invoke-Checked "browser E2E" { npm run test:e2e }
        }
        finally {
            Pop-Location
        }
    }

    Write-Host "PHASE 6C-5 OPERATIONAL E2E VERIFICATION PASSED"
}
finally {
    if ($null -eq $originalGroqApiKey) {
        Remove-Item Env:GROQ_API_KEY -ErrorAction SilentlyContinue
    }
    else {
        $env:GROQ_API_KEY = $originalGroqApiKey
    }
}
