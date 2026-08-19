param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$PileName = "Task 1 Demo - Mortgage File 001",
    [string]$WatchLocation = "/data/watch/task1-demo-001",
    [string]$RuleFile = ".\demo\task1\rules\task1_demo_rules.json"
)

$ErrorActionPreference = "Stop"

$pileBody = @{
    name = $PileName
    description = "Synthetic Task 1 agentic-system demo pile"
    watch_enabled = $true
    watch_location = $WatchLocation
} | ConvertTo-Json

$pile = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/piles" -ContentType "application/json" -Body $pileBody

$rulesJson = Get-Content -Raw $RuleFile
$ruleSet = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/piles/$($pile.pile_id)/rule-sets" -ContentType "application/json" -Body $rulesJson

Write-Host "Task 1 demo pile created"
Write-Host "pile_id:     $($pile.pile_id)"
Write-Host "rule_set_id: $($ruleSet.rule_set_id)"
Write-Host "watch:       $WatchLocation"
