<#
.SYNOPSIS
  One-line bootstrap installer for Engram.

.DESCRIPTION
  Clones (or updates) the Engram repo into $InstallDir, then runs setup.ps1
  to install Engram to %LOCALAPPDATA%\Engram, run an initial full index, and
  register the scheduled task.

  Designed to be run with:
    irm https://raw.githubusercontent.com/aasis21/engram/main/install.ps1 | iex

  With arguments:
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/aasis21/engram/main/install.ps1))) -Interval 5

.PARAMETER CheckoutDir
  Where to clone the repo. Defaults to ~/engram.

.PARAMETER Branch
  Git branch to check out. Defaults to main.

.PARAMETER InstallDir
  Forwarded to setup.ps1. Where to install Engram. Default: %LOCALAPPDATA%\Engram

.PARAMETER Interval
  Forwarded to setup.ps1. Minutes between indexing runs. Default: 10

.PARAMETER TaskName
  Forwarded to setup.ps1. Scheduled task name. Default: "Engram Indexer"

.PARAMETER NoSchedule
  Forwarded to setup.ps1. Install + initial index only; do NOT register the scheduled task.

.PARAMETER NoInitialIndex
  Forwarded to setup.ps1. Skip the initial full index.
#>
[CmdletBinding()]
param(
    [string]$CheckoutDir = (Join-Path $HOME 'engram'),
    [string]$Branch = 'main',
    [string]$InstallDir,
    [int]$Interval = 10,
    [string]$TaskName = 'Engram Indexer',
    [switch]$NoSchedule,
    [switch]$NoInitialIndex
)

$ErrorActionPreference = 'Stop'
$repo = 'https://github.com/aasis21/engram.git'

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }

Step 'Checking prerequisites'
foreach ($cmd in @('git', 'python')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        if ($cmd -eq 'python' -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
            Ok 'python (via py launcher)'
            continue
        }
        throw "$cmd not found on PATH. Install it first."
    }
    Ok "$cmd available"
}

if (Test-Path (Join-Path $CheckoutDir '.git')) {
    Step "Updating existing checkout at $CheckoutDir"
    Push-Location $CheckoutDir
    try {
        git fetch --quiet origin
        git checkout --quiet $Branch
        git pull --ff-only --quiet origin $Branch
        Ok "synced to origin/$Branch"
    } finally { Pop-Location }
} else {
    Step "Cloning $repo into $CheckoutDir"
    git clone --quiet --branch $Branch $repo $CheckoutDir
    Ok 'cloned'
}

Step 'Running setup.ps1'
Push-Location $CheckoutDir
try {
    $setupArgs = @{
        Interval = $Interval
        TaskName = $TaskName
    }
    if ($PSBoundParameters.ContainsKey('InstallDir')) { $setupArgs.InstallDir = $InstallDir }
    if ($NoSchedule)     { $setupArgs.NoSchedule = $true }
    if ($NoInitialIndex) { $setupArgs.NoInitialIndex = $true }
    & (Join-Path '.' 'setup.ps1') @setupArgs
} finally { Pop-Location }
