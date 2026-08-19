param([string]$Target = ".\var\watch\task1-demo-001\closing_disclosure.txt")
$ErrorActionPreference = "Stop"
Copy-Item .\demo\task1\update\closing_disclosure.txt $Target -Force
Write-Host "Replaced watched Closing Disclosure with Task 1 UPDATE source: $Target"
