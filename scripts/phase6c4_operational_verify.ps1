param(
    [int]$ApiPort = 8000,
    [int]$UiPort = 5173,
    [int]$TimeoutSeconds = 180,
    [switch]$DownAfter
)

$ErrorActionPreference = "Stop"

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    & docker compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($ComposeArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Wait-Until {
    param(
        [scriptblock]$Condition,
        [string]$Description
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            if (& $Condition) { return }
        }
        catch {
            # Services may still be starting; retry until the deadline.
        }
        Start-Sleep -Seconds 2
    }
    throw "Timed out waiting for $Description"
}

try {
    Write-Host "[6C-4] validating Compose configuration"
    Invoke-Compose -ComposeArgs @("config", "--quiet")

    Write-Host "[6C-4] building and starting operational stack"
    Invoke-Compose -ComposeArgs @("up", "-d", "--build")

    Wait-Until -Description "healthy REST API" -Condition {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 3
        return $health.status -eq "ok"
    }

    Wait-Until -Description "frontend proxy" -Condition {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$UiPort/" -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200 -and $response.Content -match '<div id="root">'
    }

    $running = @(& docker compose ps --status running --services)
    if ($LASTEXITCODE -ne 0) { throw "docker compose ps failed" }
    foreach ($service in @("postgres", "api", "worker", "watcher", "frontend")) {
        if ($running -notcontains $service) {
            throw "Required service is not running: $service"
        }
    }

    $publicTableCount = (& docker compose exec -T postgres psql -U task1 -d task1 -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';").Trim()
    if ($LASTEXITCODE -ne 0) { throw "public table count query failed" }
    if ($publicTableCount -ne "31") {
        throw "Expected 31 public tables after migration 008, got $publicTableCount"
    }

    $migrationCount = (& docker compose exec -T postgres psql -U task1 -d task1 -Atc "SELECT count(*) FROM task1_meta.schema_migrations;").Trim()
    if ($LASTEXITCODE -ne 0) { throw "migration metadata query failed" }
    if ($migrationCount -ne "8") {
        throw "Expected 8 recorded migrations, got $migrationCount"
    }

    Write-Host "[6C-4] verifying migration replay is idempotent"
    Invoke-Compose -ComposeArgs @("run", "--rm", "migrate")
    $migrationCountAfterReplay = (& docker compose exec -T postgres psql -U task1 -d task1 -Atc "SELECT count(*) FROM task1_meta.schema_migrations;").Trim()
    if ($LASTEXITCODE -ne 0) { throw "migration replay verification query failed" }
    if ($migrationCountAfterReplay -ne "8") {
        throw "Migration replay changed recorded migration count: $migrationCountAfterReplay"
    }

    Write-Host "[6C-4] verifying daemon restart"
    Invoke-Compose -ComposeArgs @("restart", "worker", "watcher")
    Wait-Until -Description "worker and watcher restart" -Condition {
        $services = @(& docker compose ps --status running --services)
        return ($services -contains "worker") -and ($services -contains "watcher")
    }

    Write-Host "PHASE 6C-4 OPERATIONAL VERIFICATION PASSED"
    Write-Host "UI:  http://127.0.0.1:$UiPort"
    Write-Host "API: http://127.0.0.1:$ApiPort"
}
finally {
    if ($DownAfter) {
        Write-Host "[6C-4] stopping stack"
        & docker compose down
    }
}
