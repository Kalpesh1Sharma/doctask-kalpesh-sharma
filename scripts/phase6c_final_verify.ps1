param(
    [switch]$SkipDocker,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$originalGroqApiKey = $env:GROQ_API_KEY
# Final verification must not consume live provider quota. Deterministic test providers
# cover semantic behavior; Compose health/startup does not require a live key.
$env:GROQ_API_KEY = ""

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host ("=" * 88)
    Write-Host $Title
    Write-Host ("=" * 88)
}

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    Write-Host "> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Label"
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    Write-Section "Phase 6C-6 - final documentation and migration preflight"

    $expectedMigrations = @(
        "001_schema_v2_7.sql",
        "002_run_rule_scope.sql",
        "003_update_review.sql",
        "004_idempotency_claim_generation.sql",
        "005_run_step_retry_history.sql",
        "006_document_runtime_commit_and_telemetry.sql",
        "007_watch_folder_document_classification.sql",
        "008_item_level_update_review.sql"
    )

    $actualMigrations = @(
        Get-ChildItem -LiteralPath "sql/migrations" -File -Filter "*.sql" |
            Sort-Object Name |
            ForEach-Object Name
    )

    if (($actualMigrations -join "`n") -ne ($expectedMigrations -join "`n")) {
        throw "Expected exactly migrations 001 through 008.`nActual:`n$($actualMigrations -join "`n")"
    }
    Write-Host "OK ordered migration file set is exactly 001 -> 008"

    foreach ($path in @("README.md", "ARCHITECTURE_DESIGN.md", "SCHEMA_DESIGN.md")) {
        if (-not (Test-Path -LiteralPath $path)) {
            throw "Missing final reviewer-facing document: $path"
        }
    }

    if (Test-Path -LiteralPath ".codex-context/ARCHITECTURE_DESIGN.md") {
        $rootHash = (Get-FileHash -LiteralPath "ARCHITECTURE_DESIGN.md" -Algorithm SHA256).Hash
        $codexHash = (Get-FileHash -LiteralPath ".codex-context/ARCHITECTURE_DESIGN.md" -Algorithm SHA256).Hash
        if ($rootHash -ne $codexHash) {
            throw "Root and .codex-context architecture docs are not byte-identical"
        }
        Write-Host "OK architecture docs are byte-identical"
    }

    if (Test-Path -LiteralPath ".codex-context/SCHEMA_DESIGN.md") {
        $rootHash = (Get-FileHash -LiteralPath "SCHEMA_DESIGN.md" -Algorithm SHA256).Hash
        $codexHash = (Get-FileHash -LiteralPath ".codex-context/SCHEMA_DESIGN.md" -Algorithm SHA256).Hash
        if ($rootHash -ne $codexHash) {
            throw "Root and .codex-context schema docs are not byte-identical"
        }
        Write-Host "OK schema docs are byte-identical"
    }

    $readme = Get-Content -LiteralPath "README.md" -Raw
    $requiredReadmeFragments = @(
        "docker compose up --build",
        "Groq",
        "pgvector",
        ".pdf",
        ".docx",
        ".txt",
        "LOAN_ESTIMATE",
        "CLOSING_DISCLOSURE",
        "PROMISSORY_NOTE",
        "SETTLEMENT_STATEMENT",
        "UNDERWRITING_APPROVAL",
        "change-history",
        "REVIEW_REQUIRED"
    )
    foreach ($fragment in $requiredReadmeFragments) {
        if (-not $readme.Contains($fragment)) {
            throw "README final acceptance text is missing: $fragment"
        }
    }
    Write-Host "OK README declares startup, provider, retrieval, formats, domain roles, history, and human gate"

    $architecture = Get-Content -LiteralPath "ARCHITECTURE_DESIGN.md" -Raw
    if ($architecture -match "(?i)Redis is part of the target architecture") {
        throw "Final architecture still contains the stale Redis target claim"
    }
    if ($architecture -match "(?i)LangGraph is used for workflow orchestration") {
        throw "Final architecture still contains the stale LangGraph runtime claim"
    }
    foreach ($fragment in @("hand-rolled", "PostgreSQL advisory", "Groq", "pgvector", "no Redis", "five durable stages")) {
        if (-not $architecture.Contains($fragment)) {
            throw "Final architecture is missing current-runtime statement: $fragment"
        }
    }
    Write-Host "OK architecture describes the implemented 6C runtime instead of superseded target components"

    Write-Section "Secrets and generated-output checks"

    $trackedDotEnv = @(& git ls-files -- ".env")
    if ($trackedDotEnv.Count -gt 0) {
        throw ".env must not be tracked"
    }

    $trackedGenerated = @(& git ls-files -- "frontend/node_modules" "frontend/dist" "frontend/test-results" "frontend/playwright-report")
    if ($trackedGenerated.Count -gt 0) {
        throw "Generated frontend output is tracked:`n$($trackedGenerated -join "`n")"
    }
    Write-Host "OK local secrets/generated frontend output are not tracked"

    $secretMatches = @(& git grep -n -I -E "gsk_[A-Za-z0-9_-]{20,}" -- . 2>$null)
    $grepExit = $LASTEXITCODE
    if ($grepExit -eq 0) {
        throw "Possible Groq API key found in tracked content:`n$($secretMatches -join "`n")"
    }
    if ($grepExit -gt 1) {
        throw "git grep secret scan failed with exit code $grepExit"
    }
    Write-Host "OK no Groq-key-shaped secret found in tracked content"

    if (-not $SkipDocker) {
        Write-Section "One-command operational runtime verification"
        Invoke-Checked "Phase 6C-4 Docker verification" {
            powershell -ExecutionPolicy Bypass -File .\scripts\phase6c4_operational_verify.ps1
        }

        # 6C-4 intentionally leaves the complete stack running. Stop the API/UI
        # before Playwright starts its own servers, but preserve PostgreSQL data.
        Invoke-Checked "stop full Compose stack while preserving volumes" {
            docker compose down
        }
        Invoke-Checked "start PostgreSQL for isolated/local E2E suites" {
            docker compose up -d postgres
        }
        Invoke-Checked "ensure migrations are ready" {
            docker compose run --rm migrate
        }
    }

    Write-Section "Operational behavior / hostile document / machine interfaces"
    if ($SkipFrontend) {
        Invoke-Checked "Phase 6C-5 operational E2E (frontend skipped)" {
            powershell -ExecutionPolicy Bypass -File .\scripts\phase6c5_operational_e2e_verify.ps1 -SkipFrontend
        }
    }
    else {
        Invoke-Checked "Phase 6C-5 operational E2E" {
            powershell -ExecutionPolicy Bypass -File .\scripts\phase6c5_operational_e2e_verify.ps1
        }
    }

    Write-Section "Full backend regression"
    Invoke-Checked "pytest full suite" {
        pytest -q --basetemp .pytest-phase6c-final
    }

    Write-Section "Static, dependency, and diff verification"
    Invoke-Checked "ruff check ." { ruff check . }
    Invoke-Checked "python -m pip check" { python -m pip check }
    Invoke-Checked "git diff --check" { git diff --check }

    Write-Section "Postflight"
    $publicTableCount = (& docker exec task1-postgres psql -U task1 -d task1 -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not query final public table count"
    }
    if ($publicTableCount -ne "31") {
        throw "Expected 31 public tables, found $publicTableCount"
    }
    Write-Host "OK final schema has 31 public tables"

    $migrationReceiptCount = (& docker exec task1-postgres psql -U task1 -d task1 -tAc "SELECT count(*) FROM task1_meta.schema_migrations;").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not query migration receipts"
    }
    if ($migrationReceiptCount -ne "8") {
        throw "Expected 8 migration receipts, found $migrationReceiptCount"
    }
    Write-Host "OK deployment metadata has 8 migration receipts"

    Invoke-Checked "git status --short" { git status --short }

    Write-Section "PHASE 6C FINAL VERIFICATION PASSED"
    Write-Host "6C-1 through 6C-6 implementation/runtime verification is green."
    Write-Host "Next action: update PROJECT_HANDOFF.md with the exact final counts, review the diff, then create the Phase 6C Git checkpoint."
}
finally {
    if ($null -eq $originalGroqApiKey) {
        Remove-Item Env:GROQ_API_KEY -ErrorAction SilentlyContinue
    }
    else {
        $env:GROQ_API_KEY = $originalGroqApiKey
    }
    Pop-Location
}
