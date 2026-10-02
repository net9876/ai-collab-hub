<#
.SYNOPSIS
  Install or remove a per-user scheduled task that keeps the hub token cache fresh.

.DESCRIPTION
  scripts/mcp-headers.cmd (the helper Claude Code and Codex run) only reads a
  cached header file, because both clients kill helpers after 10 seconds. This
  task runs `mcp-headers.ps1 -Refresh` hidden every 5 minutes and at logon,
  while you are logged on. The refresh calls `az` only when the cached token
  has less than 25 minutes left (az itself renews only in the last ~5 minutes,
  so a short interval avoids gaps). No password is stored; the task runs as you,
  with your normal (limited) rights.

  This is a persistent change to your Windows configuration: run it only if
  you want it, and remove it with -Uninstall.

.EXAMPLE
  pwsh -File scripts/token-refresh-task.ps1 -Install
  pwsh -File scripts/token-refresh-task.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Install,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$taskName = 'ai-collab-hub token refresh'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($taskName, 'unregister scheduled task')) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Host "Removed scheduled task '$taskName'."
        }
    } else { Write-Host "Scheduled task '$taskName' is not installed." }
    return
}
if (-not $Install) { throw 'Use -Install or -Uninstall.' }

$script = Join-Path $PSScriptRoot 'mcp-headers.ps1'
$ps = (Get-Command pwsh -ErrorAction SilentlyContinue)
if ($ps) { $ps = $ps.Source } else { $ps = (Get-Command powershell).Source }
# conhost --headless avoids a console window flashing every 5 minutes (Windows 10 1809+).
$action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless `"$ps`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" -Refresh"
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew

if ($PSCmdlet.ShouldProcess($taskName, 'register scheduled task')) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($repeat, $atLogon) `
        -Principal $principal -Settings $settings `
        -Description 'Refreshes the cached Entra token for AI Collab Hub MCP clients (scripts/mcp-headers.ps1 -Refresh).' `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed '$taskName' (every 5 min + at logon) and started it once."
    Write-Host 'Remove with: pwsh -File scripts/token-refresh-task.ps1 -Uninstall'
}
