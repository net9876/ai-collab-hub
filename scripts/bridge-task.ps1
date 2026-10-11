<#
.SYNOPSIS
  Install, remove or inspect the per-user scheduled task that runs the event bridge.

.DESCRIPTION
  scripts/bridge.py polls the hub for durable events addressed to one agent label,
  shows them (toast or console) and acknowledges them. This task starts it at logon,
  hidden (pythonw.exe from the repo's .venv), as you, with your normal (limited)
  rights, and restarts it if it exits with an error. No password is stored.

  The bridge only reads its own subscription and writes its log
  (%LOCALAPPDATA%\ai-collab-hub\bridge.log). It never runs commands, launches agents,
  accepts handoffs or touches repositories. It needs the cached token from the token
  refresh task (scripts/token-refresh-task.ps1 -Install).

  This is a persistent change to your Windows configuration: run it only if you
  want it, and remove it with -Uninstall. Nothing installs it automatically.

.EXAMPLE
  pwsh -File scripts/bridge-task.ps1 -Install                     # claude-code, toasts
  pwsh -File scripts/bridge-task.ps1 -Install -Agent codex -Interval 60
  pwsh -File scripts/bridge-task.ps1 -Status
  pwsh -File scripts/bridge-task.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [ValidateSet('claude-code', 'claude-desktop', 'codex', 'chatgpt', 'other')]
    [string]$Agent = 'claude-code',
    [ValidateSet('toast', 'console')]
    [string]$Notify = 'toast',
    [ValidateRange(15, 3600)]
    [int]$Interval = 45
)
$ErrorActionPreference = 'Stop'
$taskName = 'ai-collab-hub bridge'
$Root = Split-Path -Parent $PSScriptRoot
$logFile = Join-Path $env:LOCALAPPDATA 'ai-collab-hub\bridge.log'

if ($Status) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "Scheduled task '$taskName' is not installed." }
    else {
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Host ("Task: {0}  state={1}  last run={2}  last result={3}" -f `
                $taskName, $task.State, $info.LastRunTime, $info.LastTaskResult)
        Write-Host ("Runs: {0} {1}" -f $task.Actions[0].Execute, $task.Actions[0].Arguments)
    }
    if (Test-Path $logFile) {
        Write-Host "--- last lines of $logFile"
        Get-Content $logFile -Tail 15
    } else { Write-Host "No log yet ($logFile)." }
    return
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($taskName, 'stop and unregister scheduled task')) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Host "Removed scheduled task '$taskName'. The hub subscription stays; remove it"
            Write-Host "with event_unsubscribe(name='bridge-$Agent') if you no longer want it."
        }
    } else { Write-Host "Scheduled task '$taskName' is not installed." }
    return
}
if (-not $Install) { throw 'Use -Install, -Uninstall or -Status.' }

$pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$bridge = Join-Path $PSScriptRoot 'bridge.py'
if (-not (Test-Path $pythonw)) { throw "$pythonw not found; run scripts/setup.ps1 first." }
if (-not (Test-Path (Join-Path $env:LOCALAPPDATA "ai-collab-hub\headers-$Agent.json"))) {
    Write-Warning "No cached token for '$Agent' yet. Install the token refresh task first:"
    Write-Warning '  pwsh -File scripts/token-refresh-task.ps1 -Install'
}

# pythonw: no console window, and the bridge's exit code reaches the task scheduler.
$action = New-ScheduledTaskAction -Execute $pythonw -WorkingDirectory $Root `
    -Argument "`"$bridge`" --agent $Agent --notify $Notify --interval $Interval"
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)

if ($PSCmdlet.ShouldProcess($taskName, 'register scheduled task')) {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $atLogon `
        -Principal $principal -Settings $settings `
        -Description 'Shows AI Collab Hub events (read + acknowledge only; never runs anything). scripts/bridge.py' `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed '$taskName' (at logon, agent=$Agent, notify=$Notify, every $Interval s) and started it."
    Write-Host 'Check:  pwsh -File scripts/bridge-task.ps1 -Status'
    Write-Host 'Remove: pwsh -File scripts/bridge-task.ps1 -Uninstall'
}
