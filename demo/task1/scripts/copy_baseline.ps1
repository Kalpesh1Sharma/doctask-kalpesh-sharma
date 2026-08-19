param([string]$Target = ".\var\watch\task1-demo-001")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item .\demo\task1\baseline\closing_disclosure.txt "$Target\closing_disclosure.txt" -Force
Write-Host "Copied initial Task 1 Closing Disclosure to $Target"
