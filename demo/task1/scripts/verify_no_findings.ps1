param(
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$ApiBase = "http://127.0.0.1:8000"
)
$ErrorActionPreference = "Stop"
$review = Invoke-RestMethod -Method Get -Uri "$ApiBase/api/v1/runs/$RunId/review"
$findings = @($review.findings)
if ($review.run_status -notin @("REVIEW_REQUIRED", "COMPLETED")) {
    throw "No-findings proof requires EXAMINE to be complete; run $RunId is $($review.run_status)."
}
if ($findings.Count -ne 0) {
    throw "Expected an honest no-findings result for run $RunId, found $($findings.Count)."
}
Write-Host "PASS: run $RunId has zero findings."
Write-Host "run_status: $($review.run_status)"
Write-Host "review_complete: $($review.review_complete)"
Write-Host "commit_ready: $($review.commit_ready)"
