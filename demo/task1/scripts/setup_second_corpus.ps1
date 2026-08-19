param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$RuleFile = ".\demo\task1\rules\task1_demo_rules.json"
)
$ErrorActionPreference = "Stop"
$pileBody = @{
    name = "Task 1 Demo - Mortgage File 002"
    description = "Independent second synthetic Task 1 pile"
    watch_enabled = $true
    watch_location = "/data/watch/task1-demo-002"
} | ConvertTo-Json
$pile = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/piles" -ContentType "application/json" -Body $pileBody
$rulesJson = Get-Content -Raw $RuleFile
$ruleSet = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/piles/$($pile.pile_id)/rule-sets" -ContentType "application/json" -Body $rulesJson
New-Item -ItemType Directory -Force .\var\watch\task1-demo-002 | Out-Null
Copy-Item .\demo\task1\second_corpus\*.txt .\var\watch\task1-demo-002\ -Force
Write-Host "Second Task 1 pile created and sources copied"
Write-Host "pile_id:     $($pile.pile_id)"
Write-Host "rule_set_id: $($ruleSet.rule_set_id)"
