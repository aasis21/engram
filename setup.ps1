<#
.SYNOPSIS
    Install Engram from a local checkout - the VS Code Copilot Chat -> SQLite
    indexer - and register a Windows Scheduled Task that re-indexes
    incrementally every few minutes.

.DESCRIPTION
    Run from a cloned copy of the repo. Copies the Engram files to a stable
    location (%LOCALAPPDATA%\Engram by default), runs an initial full index,
    and registers a hidden scheduled task that runs `engram.py index` on an
    interval.

    For a one-line install without a manual clone, use install.ps1 instead:
        irm https://raw.githubusercontent.com/aasis21/engram/main/install.ps1 | iex

    Re-running is safe and idempotent: files are refreshed, the task is recreated,
    and the existing database is reused (its watermark means the next run is fast).

.PARAMETER InstallDir
    Where to install Engram. Default: %LOCALAPPDATA%\Engram

.PARAMETER Interval
    Minutes between indexing runs. Default: 10

.PARAMETER TaskName
    Scheduled task name. Default: "Engram Indexer"

.PARAMETER NoSchedule
    Install + initial index only; do NOT register the scheduled task.

.PARAMETER NoInitialIndex
    Skip the initial full index (the task will pick it up on its first run).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

.EXAMPLE
    .\setup.ps1 -Interval 5 -InstallDir D:\Tools\Engram
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Engram'),
    [int]$Interval = 10,
    [string]$TaskName = 'Engram Indexer',
    [switch]$NoSchedule,
    [switch]$NoInitialIndex
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    # Prefer a real python.exe so we can derive pythonw.exe beside it.
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $cmd) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($cmd) { return $cmd.Source }
    # Fall back to the py launcher.
    $py = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        $resolved = & $py.Source -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved.Trim() }
    }
    return $null
}

Write-Host "== Engram installer ==" -ForegroundColor Cyan

$python = Find-Python
if (-not $python) {
    Write-Error "Python 3 was not found on PATH. Install Python 3.8+ from https://python.org and re-run."
    exit 1
}
$pyDir   = Split-Path -Parent $python
$pythonw = Join-Path $pyDir 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }  # fall back to console python
Write-Host "Python   : $python"
Write-Host "Pythonw  : $pythonw"

# --- Copy files -----------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$files = @('engram.py', 'config.json', 'run.cmd', 'README.md')
foreach ($f in $files) {
    $src = Join-Path $here $f
    if (Test-Path $src) {
        Copy-Item $src -Destination $InstallDir -Force
        Write-Host "Copied   : $f"
    } elseif ($f -eq 'engram.py') {
        Write-Error "Required file missing next to installer: $f"
        exit 1
    }
}
$engram = Join-Path $InstallDir 'engram.py'

# --- Initial index --------------------------------------------------------
if (-not $NoInitialIndex) {
    $dbExists = Test-Path (Join-Path $env:USERPROFILE '.copilot\session-store-vscode-chat.db')
    if ($dbExists) {
        Write-Host "`nExisting database detected - running incremental index (only files changed since last run)..." -ForegroundColor Yellow
    } else {
        Write-Host "`nNo database yet - running initial full index (this can take a few minutes on first run)..." -ForegroundColor Yellow
    }
    & $python $engram index
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Index exited with code $LASTEXITCODE. If the database is corrupt, run: .\uninstall.ps1 -RemoveData and re-install."
    }
}

# --- Scheduled task -------------------------------------------------------
if (-not $NoSchedule) {
    Write-Host "`nRegistering scheduled task '$TaskName' (every $Interval min, hidden)..." -ForegroundColor Yellow

    $action = New-ScheduledTaskAction -Execute $pythonw `
        -Argument "`"$engram`" index" -WorkingDirectory $InstallDir

    # Anchor once at install time, then repeat every N minutes indefinitely.
    # Assigning .Repetition from a helper trigger (instead of passing
    # -RepetitionDuration) yields an open-ended schedule, so Task Scheduler
    # shows "repeat every N minutes indefinitely" and it starts running now.
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date)
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '00:00' `
        -RepetitionInterval (New-TimeSpan -Minutes $Interval)).Repetition

    $settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description ("Engram - indexes your VS Code Copilot Chat history into a local SQLite database " +
            "(~/.copilot/session-store-vscode-chat.db) so you can full-text search past conversations. " +
            "Runs incrementally every $Interval minutes (only files changed since the last run). " +
            "Source: $InstallDir | https://github.com/aasis21/engram") `
        -Force | Out-Null

    Write-Host "Task registered. Kicking off one run now..."
    Start-ScheduledTask -TaskName $TaskName
}

# --- Summary --------------------------------------------------------------
Write-Host "`n== Done ==" -ForegroundColor Green
& $python $engram status
Write-Host ""
Write-Host "Query your chats:" -ForegroundColor Cyan
Write-Host "    python `"$engram`" query `"<search text>`""
Write-Host "    python `"$engram`" status"
if (-not $NoSchedule) {
    Write-Host "Manage the task:" -ForegroundColor Cyan
    Write-Host "    Get-ScheduledTask -TaskName '$TaskName'"
    Write-Host "    .\uninstall.ps1            # remove task (keeps data)"
    Write-Host "    .\uninstall.ps1 -RemoveData  # remove task + database"
}
