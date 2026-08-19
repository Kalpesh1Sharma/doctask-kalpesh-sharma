param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$RuleFile = ".\demo\task1\rules\task1_demo_rules.json",
    [string]$WatchKey = "task1-demo-002",
    [string]$PileName = "Task 1 Demo - Mortgage File 002"
)
$ErrorActionPreference = "Stop"
$hostWatch = ".\var\watch\$WatchKey"
Remove-Item $hostWatch -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $hostWatch | Out-Null

$pileBody = @{
    name = $PileName
    description = "Independent second synthetic Task 1 pile"
    watch_enabled = $true
    watch_location = "/data/watch/$WatchKey"
} | ConvertTo-Json
$pile = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/piles" -ContentType "application/json" -Body $pileBody
$rulesJson = Get-Content -Raw $RuleFile
$ruleSet = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/piles/$($pile.pile_id)/rule-sets" -ContentType "application/json" -Body $rulesJson
Write-Host "Second Task 1 pile created; no sources copied yet"
Write-Host "pile_id:     $($pile.pile_id)"
Write-Host "rule_set_id: $($ruleSet.rule_set_id)"
Write-Host "watch_key:   $WatchKey"
Write-Host "Next: .\demo\task1\scripts\copy_second_baseline.ps1 -Target $hostWatch"
