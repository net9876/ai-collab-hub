<#
.SYNOPSIS
  Run every local check: lint, types, tests (Azurite), Terraform, secret scan.

.DESCRIPTION
  Mirrors .github/workflows/ci.yml. Optional tools (checkov, gitleaks) run when
  present and are reported as SKIPPED otherwise. Exit code is non-zero if any
  required check fails. Works in Windows PowerShell 5.1 and PowerShell 7+.

.PARAMETER Quick
  Skip the Azurite-backed test suite.
#>
[CmdletBinding()]
param([switch]$Quick)
$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $Root '.venv\Scripts'
$results = [ordered]@{}

function Invoke-Check([string]$Name, [scriptblock]$Block) {
    Write-Host "== $Name" -ForegroundColor Cyan
    & $Block
    if ($LASTEXITCODE -eq 0) { $results[$Name] = 'PASS' } else { $results[$Name] = 'FAIL' }
}

Push-Location (Join-Path $Root 'server')
try {
    Invoke-Check 'ruff lint' { & (Join-Path $venv 'ruff.exe') check src tests }
    Invoke-Check 'ruff format' { & (Join-Path $venv 'ruff.exe') format --check src tests }
    Invoke-Check 'mypy' { & (Join-Path $venv 'mypy.exe') src }
    if ($Quick) {
        Invoke-Check 'pytest (unit)' { & (Join-Path $venv 'python.exe') -m pytest -q tests/test_units.py }
    } else {
        Invoke-Check 'pytest (Azurite + MCP over HTTP)' { & (Join-Path $venv 'python.exe') -m pytest -q tests }
    }
} finally { Pop-Location }

if (Get-Command terraform -ErrorAction SilentlyContinue) {
    Invoke-Check 'terraform fmt' { & terraform fmt -check -recursive (Join-Path $Root 'infra') }
    foreach ($dir in @('infra/bootstrap', 'infra/terraform')) {
        $chdir = "-chdir=$(Join-Path $Root $dir)"
        Invoke-Check "terraform validate ($dir)" {
            & terraform @($chdir, 'init', '-backend=false', '-input=false') | Out-Null
            & terraform @($chdir, 'validate', '-no-color')
        }
    }
} else { $results['terraform'] = 'SKIPPED (not installed)' }

# checkov: repo-local venv (python -m pip install checkov into .tools\checkov-venv) or PATH.
$checkovPy = Join-Path $Root '.tools\checkov-venv\Scripts\python.exe'
$checkovArgs = @('-d', (Join-Path $Root 'infra'), '--config-file', (Join-Path $Root '.checkov.yaml'))
if (Test-Path $checkovPy) {
    Invoke-Check 'checkov (terraform)' { & $checkovPy -m checkov.main @checkovArgs }
} elseif (Get-Command checkov -ErrorAction SilentlyContinue) {
    Invoke-Check 'checkov (terraform)' { & checkov @checkovArgs }
} else { $results['checkov'] = 'SKIPPED (not installed)' }

Invoke-Check 'secret / private-ID scan' {
    & (Join-Path $venv 'python.exe') (Join-Path $Root 'scripts\scan_secrets.py') --all
}
if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
    Invoke-Check 'gitleaks' { & gitleaks dir $Root --config (Join-Path $Root '.gitleaks.toml') --no-banner }
} else { $results['gitleaks'] = 'SKIPPED (not installed; CI runs it)' }

Write-Host ''
Write-Host '== Summary' -ForegroundColor Cyan
$failed = $false
foreach ($k in $results.Keys) {
    $v = $results[$k]
    if ($v -eq 'FAIL') { $failed = $true; Write-Host ("  FAIL     {0}" -f $k) -ForegroundColor Red }
    elseif ($v -eq 'PASS') { Write-Host ("  PASS     {0}" -f $k) -ForegroundColor Green }
    else { Write-Host ("  {0}  {1}" -f $v, $k) -ForegroundColor Yellow }
}
if ($failed) { exit 1 } else { exit 0 }
