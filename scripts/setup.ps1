<#
.SYNOPSIS
  Prepare a local development environment for AI Collab Hub.

.DESCRIPTION
  Creates .venv (Python >= 3.12), installs the server with dev extras, installs
  the Azurite emulator into .tools/ (repo-local, no global npm install) and
  initialises both Terraform directories without a backend.
  Changes nothing outside the repository. Works in Windows PowerShell 5.1 and
  PowerShell 7+.

.PARAMETER SkipAzurite
  Do not install Azurite (tests then fall back to `npx azurite`).

.PARAMETER SkipTerraform
  Do not run `terraform init -backend=false`.

.EXAMPLE
  pwsh -File scripts/setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipAzurite,
    [switch]$SkipTerraform
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

function Test-Tool([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

Write-Host "== Checking prerequisites"
$py = $null
foreach ($candidate in @('python', 'py')) {
    if (Test-Tool $candidate) { $py = $candidate; break }
}
if (-not $py) { throw 'Python 3.12+ is required (https://www.python.org/downloads/).' }
$ver = & $py -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]'3.12') { throw "Python $ver found; 3.12+ is required." }
Write-Host "   python $ver"
foreach ($t in @('git', 'node', 'terraform', 'az')) {
    if (Test-Tool $t) { Write-Host "   $t found" } else { Write-Warning "$t not found (needed for: $t-related steps)" }
}

Write-Host "== Python virtual environment (.venv)"
$venvPy = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { & $py -m venv (Join-Path $Root '.venv') }
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -e "$(Join-Path $Root 'server')[dev]"
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

if (-not $SkipAzurite) {
    if (Test-Tool 'npm') {
        Write-Host "== Azurite emulator into .tools/ (repo-local)"
        & npm install --prefix (Join-Path $Root '.tools') --no-audit --no-fund --silent 'azurite@3.37.0'
        if ($LASTEXITCODE -ne 0) { throw 'npm install azurite failed' }
    } else {
        Write-Warning 'npm not found: tests will try `npx azurite` or need AZURITE_CONNECTION_STRING.'
    }
}

if (-not $SkipTerraform -and (Test-Tool 'terraform')) {
    foreach ($dir in @('infra/bootstrap', 'infra/terraform')) {
        Write-Host "== terraform init -backend=false ($dir)"
        # Arguments are passed as an array: some PowerShell configurations mangle -x=y forms.
        $tfArgs = @("-chdir=$(Join-Path $Root $dir)", 'init', '-backend=false', '-input=false')
        & terraform @tfArgs | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "terraform init failed in $dir" }
    }
}

Write-Host ''
Write-Host 'Done. Next: scripts/validate.ps1'
