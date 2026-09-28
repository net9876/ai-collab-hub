<#
.SYNOPSIS
  Print MCP request headers (JSON) with a fresh Entra access token.

.DESCRIPTION
  Used by Claude Code as `headersHelper` and by scripts/codex-collab.ps1.
  Gets a delegated token for the hub API from the Azure CLI's own login
  (`az login`); no secret is stored anywhere. Writes only the JSON object to
  stdout. Reads mcp/api settings from .collab.local.json (git-ignored), written
  by scripts/connect-*.ps1.

.PARAMETER Agent
  Agent label sent as X-Collab-Agent (claude-code, codex, ...). The server
  records it next to the verified user identity; it is self-declared.

.PARAMETER TokenOnly
  Print only the raw access token (for tools that take a bearer token env var).
#>
[CmdletBinding()]
param(
    [ValidateSet('claude-code', 'claude-desktop', 'codex', 'chatgpt', 'other')]
    [string]$Agent = 'other',
    [switch]$TokenOnly
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$cfgPath = Join-Path $Root '.collab.local.json'
if (-not (Test-Path $cfgPath)) {
    [Console]::Error.WriteLine("collab: $cfgPath missing; run scripts/connect-claude.ps1 or connect-codex.ps1")
    exit 2
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$azArgs = @('account', 'get-access-token', '--scope', $cfg.api_scope, '--query', 'accessToken', '-o', 'tsv')
if ($cfg.tenant_id) { $azArgs += @('--tenant', $cfg.tenant_id) }
$token = (& az @azArgs 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $token) {
    [Console]::Error.WriteLine('collab: could not get a token. Run `az login` (same tenant) and retry.')
    exit 3
}
$token = $token.Trim()
if ($TokenOnly) { [Console]::Out.Write($token); exit 0 }
$headers = [ordered]@{ Authorization = "Bearer $token"; 'X-Collab-Agent' = $Agent }
[Console]::Out.Write(($headers | ConvertTo-Json -Compress))
