Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $ProjectRoot

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host ("=" * 88)
    Write-Host $Title
    Write-Host ("=" * 88)
}

function Invoke-Checked([string]$Command, [string[]]$Arguments = @()) {
    Write-Host ("> {0} {1}" -f $Command, ($Arguments -join " "))
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant()
}

Write-Section "Phase 6B-7 - preflight integrity"

$HeadBefore = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to read Git HEAD" }
Write-Host "HEAD before verification: $HeadBefore"

$MigrationPaths = @(
    "sql/migrations/001_schema_v2_7.sql",
    "sql/migrations/002_run_rule_scope.sql",
    "sql/migrations/003_update_review.sql",
    "sql/migrations/004_idempotency_claim_generation.sql",
    "sql/migrations/005_run_step_retry_history.sql",
    "sql/migrations/006_document_runtime_commit_and_telemetry.sql"
)
$MigrationHashes = @{}
foreach ($Path in $MigrationPaths) {
    if (-not (Test-Path $Path)) { throw "Missing migration: $Path" }
    $MigrationHashes[$Path] = Get-Sha256 $Path
    Write-Host "OK migration present: $Path"
}

$SchemaRootHash = Get-Sha256 "SCHEMA_DESIGN.md"
$SchemaContextHash = Get-Sha256 ".codex-context/SCHEMA_DESIGN.md"
if ($SchemaRootHash -ne $SchemaContextHash) {
    throw "SCHEMA_DESIGN.md and .codex-context/SCHEMA_DESIGN.md are not byte-identical"
}
Write-Host "OK schema docs are byte-identical"

if (-not (Test-Path "frontend/package-lock.json")) {
    throw "frontend/package-lock.json is missing. Run 'npm install' in frontend and keep the generated lock file."
}
Write-Host "OK frontend/package-lock.json exists"

$TrackedGenerated = & git ls-files -- frontend/dist frontend/node_modules frontend/test-results frontend/playwright-report frontend/blob-report
if ($LASTEXITCODE -ne 0) { throw "git ls-files failed" }
if ($TrackedGenerated) {
    throw "Generated frontend output is tracked and must be removed from Git:`n$($TrackedGenerated -join "`n")"
}
Write-Host "OK generated frontend output is not tracked"

if (Test-Path "phase6b_1_interface_contract_plan.md") {
    Write-Warning "Root phase6b_1_interface_contract_plan.md is a working-plan artifact. Permanent contract docs live under docs/. Do not stage it unless intentionally desired."
}

Write-Section "PostgreSQL availability"
Invoke-Checked "docker" @("compose", "up", "-d")
$DatabaseReady = $false
for ($Attempt = 1; $Attempt -le 30; $Attempt++) {
    & docker exec task1-postgres pg_isready -U task1 -d task1 *> $null
    if ($LASTEXITCODE -eq 0) {
        $DatabaseReady = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $DatabaseReady) {
    throw "PostgreSQL did not become ready within 30 seconds"
}
Write-Host "OK PostgreSQL is ready"

Write-Section "Focused: migration chain and Phase 6A database invariants"
Invoke-Checked "pytest" @("-q", "tests/repository/test_migration_006_document_runtime_commit_and_telemetry.py")
Invoke-Checked "pytest" @("-q", "tests/repository/test_update_change_set_repository.py", "-k", "migrations_apply_to_a_clean_database_in_order")

Write-Section "Focused: Phase 6B interface contracts"
Invoke-Checked "pytest" @(
    "-q",
    "tests/unit/test_interface_contract.py",
    "tests/unit/test_rest_contract.py",
    "tests/unit/test_phase6b_core_rest.py",
    "tests/unit/test_phase6b_review_rest.py",
    "tests/unit/test_phase6b_mcp.py"
)

Write-Section "Focused: black-box REST and MCP E2E"
Invoke-Checked "pytest" @("-q", "tests/e2e/test_rest_workflow.py", "tests/e2e/test_mcp_workflow.py")

Write-Section "Frontend unit, type, build, and browser E2E"
Push-Location "frontend"
try {
    Invoke-Checked "npm" @("test")
    Invoke-Checked "npm" @("run", "check")
    Invoke-Checked "npm" @("run", "build")
    Invoke-Checked "npm" @("run", "test:e2e")
}
finally {
    Pop-Location
}

Write-Section "Full backend regression suite"
$BaseTemp = ".pytest-phase6b-final"
if (Test-Path $BaseTemp) { Remove-Item -Recurse -Force $BaseTemp }
try {
    Invoke-Checked "pytest" @("-q", "--basetemp", $BaseTemp)
}
finally {
    if (Test-Path $BaseTemp) { Remove-Item -Recurse -Force $BaseTemp }
}

Write-Section "Static, dependency, and diff verification"
Invoke-Checked "ruff" @("check", ".")
Invoke-Checked "python" @("-m", "pip", "check")
Invoke-Checked "git" @("diff", "--check")

Write-Section "Postflight integrity"
foreach ($Path in $MigrationPaths) {
    $After = Get-Sha256 $Path
    if ($After -ne $MigrationHashes[$Path]) {
        throw "Migration changed during verification: $Path"
    }
}
Write-Host "OK migrations 001-006 unchanged during verification"

$SchemaRootAfter = Get-Sha256 "SCHEMA_DESIGN.md"
$SchemaContextAfter = Get-Sha256 ".codex-context/SCHEMA_DESIGN.md"
if ($SchemaRootAfter -ne $SchemaContextAfter) {
    throw "Schema docs diverged during verification"
}
Write-Host "OK schema docs remain byte-identical"

$HeadAfter = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to read Git HEAD after verification" }
if ($HeadAfter -ne $HeadBefore) {
    throw "Git HEAD changed during verification. Before=$HeadBefore After=$HeadAfter"
}
Write-Host "OK Git HEAD unchanged: $HeadAfter"

Write-Host ""
Write-Host "Changed/untracked files for final review:"
Invoke-Checked "git" @("status", "--short")

Write-Host ""
Write-Host "Diff file list:"
Invoke-Checked "git" @("diff", "--name-only")

Write-Host ""
Write-Host "PHASE 6B VERIFICATION PASSED"
