param([string]$Target = ".\var\watch\task1-demo-001")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item .\demo\task1\baseline\loan_estimate.txt "$Target\loan_estimate.txt" -Force
Write-Host "Copied Task 1 Loan Estimate UPDATE source to $Target"
