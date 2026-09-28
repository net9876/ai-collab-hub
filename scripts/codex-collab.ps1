<#
.SYNOPSIS
  Launch Codex with a fresh hub token in COLLAB_MCP_TOKEN (this process only).

.DESCRIPTION
  The token comes from your Azure CLI login and is never written to disk by this
  script. It expires after ~60-90 minutes; restart Codex through this script to
  refresh it. Extra arguments are passed to codex unchanged.
#>
$ErrorActionPreference = 'Stop'
$token = & (Join-Path $PSScriptRoot 'mcp-headers.ps1') -Agent codex -TokenOnly
if ($LASTEXITCODE -ne 0 -or -not $token) { throw 'Could not get a hub token (run az login).' }
$env:COLLAB_MCP_TOKEN = $token
try { & codex @args } finally { Remove-Item Env:\COLLAB_MCP_TOKEN -ErrorAction SilentlyContinue }
