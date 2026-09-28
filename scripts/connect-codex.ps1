<#
.SYNOPSIS
  Connect Codex CLI to the hub: project-scoped MCP config + skills.

.DESCRIPTION
  Default (project scope; nothing outside this repository changes):
    * .collab.local.json     MCP URL, API scope, tenant (git-ignored)
    * .codex/config.toml     [mcp_servers.collab] using bearer_token_env_var
                             (git-ignored). Codex reads project config only for
                             projects you have marked as trusted.
    * .agents/skills/<name>  junctions to skills/<name> (git-ignored)
  Start Codex through scripts/codex-collab.ps1, which puts a fresh Entra token
  into COLLAB_MCP_TOKEN for that process only (tokens last ~60-90 minutes).

  -UserConfig instead appends the server to ~/.codex/config.toml (backup
  first). -UserSkills links skills into ~/.agents/skills. -Undo reverts.

.EXAMPLE
  pwsh -File scripts/connect-codex.ps1 -McpUrl https://<app>/mcp -ApiScope api://<id>/Collab.ReadWrite -TenantId <tid>
  pwsh -File scripts/codex-collab.ps1
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
if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Warning 'codex CLI not found on PATH. Config is written anyway; install Codex to use it.'
}

$log = New-Object System.Collections.ArrayList
$cfg = Write-LocalConfig $McpUrl $ApiScope $TenantId

if ($PSCmdlet.ShouldProcess('.agents/skills', 'link repo skills')) {
    New-SkillLinks (Join-Path $Root '.agents\skills') $manifest $log
}
if ($UserSkills -and $PSCmdlet.ShouldProcess('~/.agents/skills', 'link repo skills (user scope)')) {
    New-SkillLinks (Join-Path $HOME '.agents\skills') $manifest $log
}

$block = @"

# --- ai-collab-hub (managed by scripts/connect-codex.ps1) ---
[mcp_servers.collab]
url = "$($cfg.mcp_url)"
bearer_token_env_var = "COLLAB_MCP_TOKEN"
http_headers = { "X-Collab-Agent" = "codex" }
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
    Add-Content -Path $tomlPath -Value $block -Encoding UTF8
    Write-Host "  updated $tomlPath"
}

Save-Manifest $manifest $log
Write-Host ''
Write-Host 'Start Codex with: pwsh -File scripts/codex-collab.ps1   (then /mcp inside Codex to verify)'
Write-Host 'Undo: scripts/connect-codex.ps1 -Undo'
