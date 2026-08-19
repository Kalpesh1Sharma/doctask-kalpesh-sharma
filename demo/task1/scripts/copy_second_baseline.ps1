param([string]$Target = ".\var\watch\task1-demo-002")
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Target | Out-Null
Copy-Item .\demo\task1\second_corpus\closing_disclosure.txt "$Target\closing_disclosure.txt" -Force
Write-Host "Copied second-corpus Closing Disclosure INITIAL source to $Target"
