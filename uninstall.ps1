<#
.SYNOPSIS
    Uninstall Engram: remove the scheduled task, and optionally the data/files.

.PARAMETER TaskName
    Scheduled task name to remove. Default: "Engram Indexer"

.PARAMETER InstallDir
    Engram install directory. Default: %LOCALAPPDATA%\Engram

.PARAMETER RemoveData
    Also delete the database and the installed files.

.EXAMPLE
    .\uninstall.ps1
    .\uninstall.ps1 -RemoveData
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Engram Indexer',
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Engram'),
    [switch]$RemoveData
)

$ErrorActionPreference = 'Stop'

Write-Host "== Engram uninstaller ==" -ForegroundColor Cyan

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task: $TaskName"
} else {
    Write-Host "No scheduled task named '$TaskName' found."
}

if ($RemoveData) {
    if (Test-Path $InstallDir) {
        Remove-Item -Recurse -Force $InstallDir
        Write-Host "Removed install directory + database: $InstallDir"
    } else {
        Write-Host "Install directory not found: $InstallDir"
    }
} else {
    Write-Host "Kept data + files at: $InstallDir"
    Write-Host "(Re-run with -RemoveData to delete the database and installed files.)"
}

Write-Host "== Done ==" -ForegroundColor Green
