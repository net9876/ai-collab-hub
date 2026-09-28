# Shared helpers for scripts/connect-*.ps1. Every change is recorded in a
# manifest under .collab/ so `-Undo` can reverse exactly what was done.
# Compatible with Windows PowerShell 5.1 and PowerShell 7+.

$script:Root = Split-Path -Parent $PSScriptRoot

function Get-CollabRoot { return $script:Root }

function Read-Manifest([string]$Name) {
    $path = Join-Path $script:Root ".collab\$Name.json"
    if (Test-Path $path) { return @(Get-Content $path -Raw | ConvertFrom-Json) }
    return @()
}

function Save-Manifest([string]$Name, $Entries) {
    $dir = Join-Path $script:Root '.collab'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    ConvertTo-Json -InputObject @($Entries) -Depth 5 | Set-Content (Join-Path $dir "$Name.json") -Encoding UTF8
}

function Write-LocalConfig([string]$McpUrl, [string]$ApiScope, [string]$TenantId) {
    $path = Join-Path $script:Root '.collab.local.json'
    $cfg = [ordered]@{ mcp_url = $McpUrl; api_scope = $ApiScope; tenant_id = $TenantId }
    if (Test-Path $path) {
        $old = Get-Content $path -Raw | ConvertFrom-Json
        if (-not $McpUrl) { $cfg.mcp_url = $old.mcp_url }
        if (-not $ApiScope) { $cfg.api_scope = $old.api_scope }
        if (-not $TenantId) { $cfg.tenant_id = $old.tenant_id }
    }
    if (-not $cfg.mcp_url -or -not $cfg.api_scope) {
        throw 'Pass -McpUrl and -ApiScope (terraform outputs mcp_url and api_scope) on first run.'
    }
    if ($cfg.mcp_url -notmatch '^https://[^/]+/mcp$' -and $cfg.mcp_url -notmatch '^http://127\.0\.0\.1:\d+/mcp$') {
        throw "McpUrl must look like https://<host>/mcp, got '$($cfg.mcp_url)'."
    }
    $cfg | ConvertTo-Json | Set-Content $path -Encoding UTF8
    return $cfg
}

function New-SkillLinks([string]$TargetDir, [string]$ManifestName, [System.Collections.ArrayList]$Log) {
    # Directory junctions need no admin rights and keep skills/ as the single source.
    if (-not (Test-Path $TargetDir)) {
        New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
        [void]$Log.Add(@{ kind = 'dir'; path = $TargetDir })
    }
    foreach ($skill in Get-ChildItem (Join-Path $script:Root 'skills') -Directory) {
        $link = Join-Path $TargetDir $skill.Name
        if (Test-Path $link) {
            $item = Get-Item $link -Force
            if ($item.LinkType -eq 'Junction' -and $item.Target -contains $skill.FullName) { continue }
            Write-Warning "Skipping '$link': something else already exists there (left untouched)."
            continue
        }
        New-Item -ItemType Junction -Path $link -Target $skill.FullName | Out-Null
        [void]$Log.Add(@{ kind = 'junction'; path = $link })
        Write-Host "  linked $link -> $($skill.FullName)"
    }
}

function Backup-File([string]$Path, [System.Collections.ArrayList]$Log) {
    if (Test-Path $Path) {
        $bak = "$Path.bak-$(Get-Date -Format yyyyMMddHHmmss)"
        Copy-Item $Path $bak
        [void]$Log.Add(@{ kind = 'backup'; path = $Path; backup = $bak })
        Write-Host "  backup $bak"
    } else {
        [void]$Log.Add(@{ kind = 'created'; path = $Path })
    }
}

function Undo-Manifest([string]$Name) {
    $entries = Read-Manifest $Name
    if (-not $entries -or $entries.Count -eq 0) { Write-Host "Nothing to undo for $Name."; return }
    [array]::Reverse($entries)
    foreach ($e in $entries) {
        switch ($e.kind) {
            'junction' {
                if (Test-Path $e.path) { (Get-Item $e.path -Force).Delete(); Write-Host "  removed link $($e.path)" }
            }
            'dir' {
                if ((Test-Path $e.path) -and -not (Get-ChildItem $e.path -Force)) {
                    Remove-Item $e.path; Write-Host "  removed empty dir $($e.path)"
                }
            }
            'backup' { Copy-Item $e.backup $e.path -Force; Write-Host "  restored $($e.path) from $($e.backup)" }
            'created' {
                if (Test-Path $e.path) { Remove-Item $e.path; Write-Host "  removed $($e.path)" }
            }
            'command' { Write-Host "  manual step to undo: $($e.undo)" }
        }
    }
    Remove-Item (Join-Path $script:Root ".collab\$Name.json")
}

Export-ModuleMember -Function Get-CollabRoot, Read-Manifest, Save-Manifest, Write-LocalConfig, `
    New-SkillLinks, Backup-File, Undo-Manifest
