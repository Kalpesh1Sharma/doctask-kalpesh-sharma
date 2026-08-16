param(
    [switch]$SkipDbReset
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host ('=' * 88)
    Write-Host $Title
    Write-Host ('=' * 88)
}

function Assert-LastExitCode([string]$What) {
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Python([string[]]$Arguments, [string]$Label) {
    & $script:Python @Arguments
    Assert-LastExitCode $Label
}

function Assert-Sha256([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing required file: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected.ToLowerInvariant()) {
        throw "SHA-256 mismatch for $Path`nExpected: $Expected`nActual:   $actual"
    }
    Write-Host "OK SHA-256  $Path"
}

if (-not (Test-Path -LiteralPath 'pyproject.toml')) {
    throw 'Run this script from the D:\project repository root.'
}

$script:Python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $script:Python)) {
    throw "Virtualenv Python not found at $script:Python"
}

$baselineHashes = [ordered]@{
    'sql\migrations\001_schema_v2_7.sql' = '6bf0fdf35af0c5e0a272e60fe13cad24377a4a041a4fc4d03a18c1a28a24f3e3'
    'sql\migrations\002_run_rule_scope.sql' = '807f54b963537963fbfb620dfcedc640a1d8756d0dbe959781cffae7bba70659'
    'sql\migrations\003_update_review.sql' = 'db04b291bb819c72e2f4ed9072b76f2dbd6f66eec26b7ba974449a1f92ce50ac'
    'sql\migrations\004_idempotency_claim_generation.sql' = '05765ed75424eadd84f72958e6c33213a8684533fe44e745e578f73f78694124'
    'sql\migrations\005_run_step_retry_history.sql' = '106760a6ccd115bdb65273beaf45b51ae97370ed026b0e59660a68b38736e80b'
    '.codex-context\PROJECT_HANDOFF.md' = 'ea58be1597fe0552f40472bbf7afaecb7fdb216c274bb9e6d8923e6759311b76'
}

Write-Section 'Phase 6A Issue 14 — preflight integrity'
$headBefore = (& git rev-parse HEAD).Trim()
Assert-LastExitCode 'git rev-parse HEAD'
Write-Host "HEAD before verification: $headBefore"

foreach ($entry in $baselineHashes.GetEnumerator()) {
    Assert-Sha256 $entry.Key $entry.Value
}

$rootSchemaHash = (Get-FileHash -LiteralPath 'SCHEMA_DESIGN.md' -Algorithm SHA256).Hash
$codexSchemaHash = (Get-FileHash -LiteralPath '.codex-context\SCHEMA_DESIGN.md' -Algorithm SHA256).Hash
if ($rootSchemaHash -ne $codexSchemaHash) {
    throw 'SCHEMA_DESIGN.md and .codex-context\SCHEMA_DESIGN.md are not byte-identical.'
}
Write-Host 'OK byte-identical schema docs'

if (-not $SkipDbReset) {
    Write-Section 'Rebuild local PostgreSQL from final migrations 001 -> 006'
    Write-Host 'This intentionally deletes the local docker-compose PostgreSQL volume.'
    & docker compose down -v
    Assert-LastExitCode 'docker compose down -v'
    & docker compose up -d postgres
    Assert-LastExitCode 'docker compose up -d postgres'

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        & docker exec task1-postgres pg_isready -U task1 -d task1 *> $null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw 'PostgreSQL did not become ready within 60 seconds.'
    }
    Write-Host 'PostgreSQL is ready.'

    $migrations = @(
        'sql\migrations\001_schema_v2_7.sql',
        'sql\migrations\002_run_rule_scope.sql',
        'sql\migrations\003_update_review.sql',
        'sql\migrations\004_idempotency_claim_generation.sql',
        'sql\migrations\005_run_step_retry_history.sql',
        'sql\migrations\006_document_runtime_commit_and_telemetry.sql'
    )
    foreach ($migration in $migrations) {
        Write-Host "Applying $migration"
        Get-Content -LiteralPath $migration -Raw | & docker exec -i task1-postgres psql -U task1 -d task1 -v ON_ERROR_STOP=1
        Assert-LastExitCode "apply $migration"
    }

    $tableCount = (& docker exec task1-postgres psql -U task1 -d task1 -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = 'public';").Trim()
    Assert-LastExitCode 'public table count query'
    if ($tableCount -ne '27') {
        throw "Expected exactly 27 public tables after Migration 006, got $tableCount"
    }
    Write-Host 'OK final schema has exactly 27 public tables'
}
else {
    Write-Section 'Database reset skipped by request'
}

Write-Section 'Focused: document runtime, storage, parsers'
Invoke-Python @('-m','pytest','-q','tests/unit/test_document_runtime.py','tests/unit/test_local_artifact_storage.py') 'document runtime/storage/parser tests'

Write-Section 'Focused: ingestion + arrival + fencing/concurrency'
Invoke-Python @('-m','pytest','-q','tests/application/test_document_runtime_services.py') 'document runtime application tests'

Write-Section 'Focused: deterministic renderer + focused UPDATE proof'
Invoke-Python @('-m','pytest','-q','tests/unit/test_final_report_renderer.py','tests/application/test_update_review_services.py') 'renderer/update tests'

Write-Section 'Focused: COMMIT finalization + replay/crash matrix'
Invoke-Python @('-m','pytest','-q','tests/application/test_commit_final_report_service.py','tests/application/test_finalize_run_service.py') 'COMMIT/finalization tests'

Write-Section 'Focused: five-stage workflow + Phase 5C retry history regressions'
Invoke-Python @('-m','pytest','-q','tests/application/test_run_workflow_service.py') 'workflow tests'

Write-Section 'Focused: production provider adapters + Phase 5B ownership/error regressions'
Invoke-Python @(
    '-m','pytest','-q',
    'tests/unit/test_openai_task_providers.py',
    'tests/application/test_extract_run_facts_service.py',
    'tests/application/test_detect_run_contradictions_service.py',
    'tests/application/test_evaluate_run_rules_service.py',
    'tests/repository/test_idempotency_repository.py'
) 'provider/Phase 5B tests'

Write-Section 'Focused: provider usage repository + aggregation'
Invoke-Python @('-m','pytest','-q','tests/repository/test_provider_usage_repository.py','tests/application/test_provider_usage_tracking.py') 'provider usage tests'

Write-Section 'Focused: Migration 006 + clean ordered migration chain'
Invoke-Python @('-m','pytest','-q','tests/repository/test_migration_006_document_runtime_commit_and_telemetry.py') 'Migration 006 tests'
Invoke-Python @('-m','pytest','-q','tests/repository/test_update_change_set_repository.py','-k','migrations_apply_to_a_clean_database_in_order') 'clean 001->006 migration-chain test'

Write-Section 'Full regression suite'
if (Test-Path -LiteralPath '.pytest-phase6a-final') {
    Remove-Item -LiteralPath '.pytest-phase6a-final' -Recurse -Force
}
Invoke-Python @('-m','pytest','-q','--basetemp','.pytest-phase6a-final') 'full pytest suite'
if (Test-Path -LiteralPath '.pytest-phase6a-final') {
    Remove-Item -LiteralPath '.pytest-phase6a-final' -Recurse -Force
}

Write-Section 'Static/dependency/diff verification'
Invoke-Python @('-m','ruff','check','.') 'ruff check'
Invoke-Python @('-m','pip','check') 'pip check'
& git diff --check
Assert-LastExitCode 'git diff --check'

Write-Section 'Postflight immutable-history/document checks'
foreach ($entry in $baselineHashes.GetEnumerator()) {
    Assert-Sha256 $entry.Key $entry.Value
}
$rootSchemaHash = (Get-FileHash -LiteralPath 'SCHEMA_DESIGN.md' -Algorithm SHA256).Hash
$codexSchemaHash = (Get-FileHash -LiteralPath '.codex-context\SCHEMA_DESIGN.md' -Algorithm SHA256).Hash
if ($rootSchemaHash -ne $codexSchemaHash) {
    throw 'SCHEMA_DESIGN.md and .codex-context\SCHEMA_DESIGN.md diverged during verification.'
}
Write-Host 'OK byte-identical schema docs'

$headAfter = (& git rev-parse HEAD).Trim()
Assert-LastExitCode 'git rev-parse HEAD (postflight)'
if ($headAfter -ne $headBefore) {
    throw "Git HEAD changed during verification.`nBefore: $headBefore`nAfter:  $headAfter"
}
Write-Host "OK no commit created during verification (HEAD unchanged: $headAfter)"

Write-Section 'Final worktree report — paste this section back into ChatGPT'
Write-Host 'git status --short:'
& git status --short
Assert-LastExitCode 'git status --short'
Write-Host ''
Write-Host 'git diff --name-only:'
& git diff --name-only
Assert-LastExitCode 'git diff --name-only'
Write-Host ''
Write-Host 'git diff --stat:'
& git diff --stat
Assert-LastExitCode 'git diff --stat'

Write-Section 'ISSUE 14 VERIFICATION PASSED'
Write-Host 'All required focused checks, clean migrations, 27-table schema, full regression, Ruff, pip check, diff check, immutable history hashes, schema-doc identity, handoff preservation, and no-new-commit check passed.'
