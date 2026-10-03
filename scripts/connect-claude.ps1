<#
.SYNOPSIS
  Connect Claude Code to the hub: project-scoped MCP server + skills.

.DESCRIPTION
  Default (project scope; nothing outside this repository changes):
    * .collab.local.json      MCP URL, API scope, tenant (git-ignored)
    * .mcp.json               "collab" HTTP server with a headersHelper that fetches
                              an Entra token via `az` on each connect (git-ignored)
    * .claude/skills/<name>   junctions to skills/<name> (git-ignored)
  Claude Code asks you to approve project MCP servers / headersHelper the first
  time (trust prompt); approve only if you recognise this repository.

  -UserSkills also links the skills into ~/.claude/skills (all projects).
  Every change is recorded in .collab/connect-claude.json; -Undo reverts it.

.EXAMPLE
  pwsh -File scripts/connect-claude.ps1 -McpUrl https://<app>.<env>.eastus.azurecontainerapps.io/mcp `
       -ApiScope api://<api-client-id>/Collab.ReadWrite -TenantId <tenant-id>
  pwsh -File scripts/connect-claude.ps1 -Undo
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$McpUrl,
    [string]$ApiScope,
    [string]$TenantId,
    [switch]$UserSkills,
    [switch]$Undo
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'CollabConnect.psm1') -Force
$Root = Get-CollabRoot
$manifest = 'connect-claude'

if ($Undo) { Undo-Manifest $manifest; return }
if ((Read-Manifest $manifest).Count -gt 0) {
    throw 'Already connected (see .collab/connect-claude.json). Run with -Undo first to reconnect.'
}

$log = New-Object System.Collections.ArrayList
$cfg = Write-LocalConfig $McpUrl $ApiScope $TenantId
Write-Host "MCP URL: $($cfg.mcp_url)"

if ($PSCmdlet.ShouldProcess('.claude/skills', 'link repo skills')) {
    New-SkillLinks (Join-Path $Root '.claude\skills') $manifest $log
}
if ($UserSkills -and $PSCmdlet.ShouldProcess('~/.claude/skills', 'link repo skills (user scope)')) {
    New-SkillLinks (Join-Path $HOME '.claude\skills') $manifest $log
}

$mcpPath = Join-Path $Root '.mcp.json'
if ($PSCmdlet.ShouldProcess($mcpPath, 'add collab MCP server')) {
    Backup-File $mcpPath $log
    $doc = [ordered]@{ mcpServers = [ordered]@{} }
    if (Test-Path $mcpPath) {
        $existing = Get-Content $mcpPath -Raw | ConvertFrom-Json
        foreach ($p in $existing.mcpServers.PSObject.Properties) { $doc.mcpServers[$p.Name] = $p.Value }
    }
    # Fast cached helper: Claude Code kills header helpers after 10 seconds, and
    # PowerShell + az can take longer (see scripts/mcp-headers.cmd).
    $helper = Join-Path $Root 'scripts\mcp-headers.cmd'
    $doc.mcpServers['collab'] = [ordered]@{
        type          = 'http'
        url           = $cfg.mcp_url
        headersHelper = "`"$helper`" claude-code"
    }
    $doc | ConvertTo-Json -Depth 6 | Set-Content $mcpPath -Encoding UTF8
    Write-Host "  wrote $mcpPath"
}

Save-Manifest $manifest $log
Write-Host ''
Write-Host 'Verify: in this folder run `claude mcp list` (expect collab: connected), or /mcp inside Claude Code.'
Write-Host 'Needs a current `az login` in the hub tenant. Undo: scripts/connect-claude.ps1 -Undo'
