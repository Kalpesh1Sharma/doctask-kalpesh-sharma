param([string]$Target = ".\var\watch\task1-demo-002")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item .\demo\task1\second_corpus\loan_estimate.txt "$Target\loan_estimate.txt" -Force
Write-Host "Copied second-corpus Loan Estimate UPDATE source to $Target"
