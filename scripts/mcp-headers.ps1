<#
.SYNOPSIS
  Get hub request headers backed by an Entra token from the Azure CLI.

.DESCRIPTION
  Three modes:
    (default)   print {"Authorization": "...", "X-Collab-Agent": "<agent>"} after a
                direct `az account get-access-token` call (slow: seconds).
    -TokenOnly  print only the raw access token (direct call).
    -Refresh    refresh the local cache used by scripts/mcp-headers.cmd, the fast
                helper that Claude Code and Codex run (both give header helpers
                only 10 seconds). Exits immediately if the cached token is still
                valid for more than 25 minutes or another refresh is running.

  Cache: %LOCALAPPDATA%\ai-collab-hub (ACL: current user only). It holds one
  access token (lifetime ~60-90 min, audience = this hub only) and per-agent
  header files. Reads mcp/api settings from .collab.local.json (git-ignored),
  written by scripts/connect-*.ps1. Works in Windows PowerShell 5.1 and 7+.
#>
[CmdletBinding()]
param(
    [ValidateSet('claude-code', 'claude-desktop', 'codex', 'chatgpt', 'other')]
    [string]$Agent = 'other',
    [switch]$TokenOnly,
    [switch]$Refresh
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$cfgPath = Join-Path $Root '.collab.local.json'
$agents = @('claude-code', 'claude-desktop', 'codex', 'chatgpt', 'other')
$cacheDir = Join-Path $env:LOCALAPPDATA 'ai-collab-hub'

function Get-Config {
    if (-not (Test-Path $cfgPath)) {
        [Console]::Error.WriteLine("collab: $cfgPath missing; run scripts/connect-claude.ps1 or connect-codex.ps1")
        exit 2
    }
    return Get-Content $cfgPath -Raw | ConvertFrom-Json
}

function Get-AzToken($cfg) {
    $azArgs = @('account', 'get-access-token', '--scope', $cfg.api_scope,
        '--query', '{t:accessToken, e:expires_on}', '-o', 'json')
    if ($cfg.tenant_id) { $azArgs += @('--tenant', $cfg.tenant_id) }
    $raw = (& az @azArgs 2>$null) -join ''
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        [Console]::Error.WriteLine('collab: could not get a token. Run `az login` (same tenant) and retry.')
        exit 3
    }
    return $raw | ConvertFrom-Json
}

function Write-Atomic([string]$Path, [string]$Text) {
    $tmp = "$Path.$PID.tmp"
    [System.IO.File]::WriteAllText($tmp, $Text, (New-Object System.Text.UTF8Encoding $false))
    Move-Item -Force $tmp $Path
}

function Initialize-CacheDir {
    if (-not (Test-Path $cacheDir)) {
        New-Item -ItemType Directory -Path $cacheDir | Out-Null
        # Current user only (no inherited access for other accounts).
        $null = & icacls $cacheDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" 2>&1
    }
}

if ($Refresh) {
    Initialize-CacheDir
    $tokenFile = Join-Path $cacheDir 'token.json'
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if (Test-Path $tokenFile) {
        try {
            $cached = Get-Content $tokenFile -Raw | ConvertFrom-Json
            if (([int64]$cached.e - $now) -gt 1500) { exit 0 }   # > 25 min left
        } catch { }
    }
    # One refresher at a time; a lock older than 2 minutes is considered stale.
    $lock = Join-Path $cacheDir 'refresh.lock'
    if ((Test-Path $lock) -and ((Get-Item $lock).LastWriteTimeUtc -gt (Get-Date).ToUniversalTime().AddMinutes(-2))) { exit 0 }
    Set-Content -Path $lock -Value $PID
    try {
        $tok = Get-AzToken (Get-Config)
        foreach ($a in $agents) {
            $h = [ordered]@{ Authorization = "Bearer $($tok.t)"; 'X-Collab-Agent' = $a }
            Write-Atomic (Join-Path $cacheDir "headers-$a.json") ($h | ConvertTo-Json -Compress)
        }
        Write-Atomic $tokenFile (@{ e = [int64]$tok.e } | ConvertTo-Json -Compress)  # expiry only
    } finally {
        Remove-Item $lock -ErrorAction SilentlyContinue
    }
    exit 0
}

$tok = Get-AzToken (Get-Config)
if ($TokenOnly) { [Console]::Out.Write($tok.t); exit 0 }
$headers = [ordered]@{ Authorization = "Bearer $($tok.t)"; 'X-Collab-Agent' = $Agent }
[Console]::Out.Write(($headers | ConvertTo-Json -Compress))
