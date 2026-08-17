$ErrorActionPreference = 'Stop'

function Run-Checked([string]$Command) {
    Write-Host "> $Command"
    Invoke-Expression $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

Write-Host "`n=== Groq provider focused tests ==="
Run-Checked 'pytest -q tests/unit/test_groq_structured_transport.py tests/unit/test_groq_task_providers.py'
Run-Checked 'pytest -q tests/application/test_provider_usage_tracking.py'

Write-Host "`n=== Existing provider classification regressions ==="
Run-Checked 'pytest -q tests/unit/test_openai_task_providers.py tests/unit/test_document_runtime.py -k "structured_transport or deterministic_embedding"'
Run-Checked 'pytest -q tests/application/test_extract_run_facts_service.py -k "structured_invalid"'
Run-Checked 'pytest -q tests/application/test_detect_run_contradictions_service.py -k "structured_invalid"'
Run-Checked 'pytest -q tests/application/test_evaluate_run_rules_service.py -k "structured_invalid"'

Write-Host "`n=== Full backend regression ==="
Run-Checked 'pytest -q --basetemp .pytest-groq-provider-final'
Run-Checked 'ruff check .'
Run-Checked 'python -m pip check'
Run-Checked 'git diff --check'

Write-Host "`nGROQ PROVIDER VERIFICATION PASSED"
