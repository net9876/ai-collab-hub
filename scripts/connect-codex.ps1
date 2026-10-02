<#
.SYNOPSIS
  Connect Codex (ChatGPT desktop app or Codex CLI) to the hub: MCP config + skills.

.DESCRIPTION
  Default (project scope; nothing outside this repository changes):
    * .collab.local.json     MCP URL, API scope, tenant (git-ignored)
    * .codex/config.toml     [mcp_servers.collab] with http_headers_helper, which
                             runs scripts/mcp-headers.ps1 to get a fresh Entra
                             token from `az` on each connection (git-ignored).
                             Codex reads project config only for trusted projects:
                             open C:\AI\collab as a project in Codex and trust it.
    * .agents/skills/<name>  junctions to skills/<name> (git-ignored)
  The ChatGPT desktop app and the Codex CLI share ~/.codex and the project's
  .codex/config.toml, so the same setup serves both.

  -UserConfig instead appends the server to ~/.codex/config.toml (backup
  first; makes the hub available in every Codex project). -UserSkills links
  skills into ~/.agents/skills. -Undo reverts.

.EXAMPLE
  pwsh -File scripts/connect-codex.ps1 -McpUrl https://<app>/mcp -ApiScope api://<id>/Collab.ReadWrite -TenantId <tid>
  pwsh -File scripts/connect-codex.ps1      # reuses .collab.local.json
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$McpUrl,
    [string]$ApiScope,
    [string]$TenantId,
    [switch]$UserConfig,
    [switch]$UserSkills,
    [switch]$Undo
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'CollabConnect.psm1') -Force
$Root = Get-CollabRoot
$manifest = 'connect-codex'

if ($Undo) { Undo-Manifest $manifest; return }
if ((Read-Manifest $manifest).Count -gt 0) {
    throw 'Already connected (see .collab/connect-codex.json). Run with -Undo first to reconnect.'
}

$log = New-Object System.Collections.ArrayList
$cfg = Write-LocalConfig $McpUrl $ApiScope $TenantId

if ($PSCmdlet.ShouldProcess('.agents/skills', 'link repo skills')) {
    New-SkillLinks (Join-Path $Root '.agents\skills') $manifest $log
}
if ($UserSkills -and $PSCmdlet.ShouldProcess('~/.agents/skills', 'link repo skills (user scope)')) {
    New-SkillLinks (Join-Path $HOME '.agents\skills') $manifest $log
}

# pwsh if available, else Windows PowerShell; the helper is compatible with both.
$shell = 'powershell'
if (Get-Command pwsh -ErrorAction SilentlyContinue) { $shell = 'pwsh' }
$helper = Join-Path $Root 'scripts\mcp-headers.ps1'
# TOML literal string ('...'): backslashes and double quotes need no escaping.
$helperCmd = "$shell -NoProfile -ExecutionPolicy Bypass -File `"$helper`" -Agent codex"

$block = @"

# --- ai-collab-hub (managed by scripts/connect-codex.ps1) ---
[mcp_servers.collab]
url = "$($cfg.mcp_url)"
http_headers_helper = '$helperCmd'
startup_timeout_sec = 60
# --- end ai-collab-hub ---
"@

if ($UserConfig) { $tomlPath = Join-Path $HOME '.codex\config.toml' }
else { $tomlPath = Join-Path $Root '.codex\config.toml' }

if ($PSCmdlet.ShouldProcess($tomlPath, 'add [mcp_servers.collab]')) {
    $dir = Split-Path -Parent $tomlPath
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
        [void]$log.Add(@{ kind = 'dir'; path = $dir })
    }
    if ((Test-Path $tomlPath) -and ((Get-Content $tomlPath -Raw) -match '\[mcp_servers\.collab\]')) {
        throw "$tomlPath already defines [mcp_servers.collab]; remove it or run -Undo first."
    }
    Backup-File $tomlPath $log
    # UTF-8 without BOM in both PowerShell 5.1 and 7 (a BOM can break TOML parsers).
    [System.IO.File]::AppendAllText($tomlPath, $block, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "  updated $tomlPath"
}

Save-Manifest $manifest $log
Write-Host ''
Write-Host 'Next: open this folder as a project in Codex (ChatGPT app or CLI), trust it,'
Write-Host 'then check Settings > MCP servers (app) or /mcp (CLI). Undo: scripts/connect-codex.ps1 -Undo'
