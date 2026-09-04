#Requires -Version 5.1
<#
.SYNOPSIS
    Register a Windows Task Scheduler job that runs cswap-token-refresher.ps1 once a day.

.DESCRIPTION
    Creates (or replaces) a scheduled task named "cswap token refresher" that runs
    cswap-token-refresher.ps1 from this repo every day at a time you choose
    (default 4:30 AM).

    The task is configured with:
      -StartWhenAvailable     run as soon as possible after a missed start
                              (e.g. the machine was asleep / powered off)
      -RunOnlyIfNetworkAvailable   only start when a network connection is present

    Run this from a normal PowerShell prompt:

        powershell -ExecutionPolicy Bypass -File .\setup.ps1

    Registering a per-user task does not require elevation; if you hit an
    access-denied error, re-run from an elevated prompt.
#>

[CmdletBinding()]
param(
    # Skip the prompt and use this time directly, e.g. -At "4:30am" or -At "06:00".
    [string]$At,

    # Task name to register under.
    [string]$TaskName = "cswap token refresher",

    # Window style for the powershell.exe the task launches.
    #   Minimized - starts minimized to the taskbar
    #   Hidden    - no visible window at all (default)
    #   Normal    - ordinary console window
    [ValidateSet('Minimized', 'Hidden', 'Normal')]
    [string]$WindowStyle = 'Hidden',

    # Directory for per-run log files. Defaults to <repo>\logs.
    [string]$LogDir,

    # Delete log files older than this many days at the start of each run.
    [int]$LogRetentionDays = 14,

    # Register the task without any logging.
    [switch]$NoLog,

    # Trigger the task once immediately after registering, without prompting.
    [switch]$RunNow
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
$targetPath = Join-Path $scriptDir "cswap-token-refresher.ps1"

if (-not (Test-Path -LiteralPath $targetPath)) {
    throw "Cannot find cswap-token-refresher.ps1 next to this script (looked in '$scriptDir')."
}

if (-not $LogDir) { $LogDir = Join-Path $scriptDir "logs" }

function Parse-TimeOfDay {
    param([string]$Text)

    $t = ($Text -replace '\s', '').ToUpper()
    $formats = @('h:mmtt', 'hh:mmtt', 'htt', 'hhtt', 'H:mm', 'HH:mm', 'H', 'HH')
    $invariant = [System.Globalization.CultureInfo]::InvariantCulture
    $style = [System.Globalization.DateTimeStyles]::None
    foreach ($fmt in $formats) {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParseExact($t, $fmt, $invariant, $style, [ref]$parsed)) {
            return $parsed
        }
    }
    return $null
}

# --- Work out the run time -------------------------------------------------------
$defaultTime = "4:30am"

if (-not $At) {
    $answer = Read-Host "What time should 'cswap token refresher' run each day? [$defaultTime]"
    if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $defaultTime }
}
else {
    $answer = $At
}

$runAt = Parse-TimeOfDay $answer
if (-not $runAt) {
    throw "Could not understand the time '$answer'. Try formats like '4:30am', '04:30', or '16:30'."
}

Write-Host ("Scheduling daily run at {0:h:mm tt}." -f $runAt)

# --- Replace any existing task ------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $reply = Read-Host "A task named '$TaskName' already exists. Replace it? [Y/n]"
    if ($reply -and $reply.Trim().ToLower().StartsWith('n')) {
        Write-Host "Left the existing task untouched. Nothing to do."
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the old task."
}

# --- Build and register --------------------------------------------------------
$scriptArgs = ""
if (-not $NoLog) {
    $scriptArgs = " -LogDir `"$LogDir`" -LogRetentionDays $LogRetentionDays"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -WindowStyle {0} -ExecutionPolicy Bypass -File `"{1}`"{2}" -f $WindowStyle, $targetPath, $scriptArgs) `
    -WorkingDirectory $scriptDir

$trigger = New-ScheduledTaskTrigger -Daily -At $runAt

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Exercise cswap-managed Claude profile refresh tokens daily" `
    -RunLevel Limited | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'."
Write-Host ("  runs daily at : {0:h:mm tt}" -f $runAt)
Write-Host "  script        : $targetPath"
Write-Host "  catch-up      : runs as soon as possible after a missed start"
Write-Host "  network       : only starts when a connection is available"
Write-Host "  battery       : allowed to start and keep running on battery power"
Write-Host "  window        : powershell.exe launches $WindowStyle"
if ($NoLog) {
    Write-Host "  logging       : disabled"
}
else {
    Write-Host "  logging       : $LogDir  (per-run files, pruned after $LogRetentionDays days)"
}
Write-Host ""

# --- Offer to run it once now ------------------------------------------------
$runIt = $RunNow
if (-not $RunNow) {
    $reply = Read-Host "Run '$TaskName' now to test it? [y/N]"
    $runIt = $reply -and $reply.Trim().ToLower().StartsWith('y')
}

if ($runIt) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started '$TaskName'. Check the result with:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName `"$TaskName`""
    if (-not $NoLog) {
        Write-Host "  Get-Content (Get-ChildItem `"$LogDir`" -Filter 'cswap-refresher_*.log' | Sort-Object LastWriteTime | Select-Object -Last 1).FullName"
    }
    Write-Host ""
}

Write-Host "Handy follow-ups:"
Write-Host "  Start-ScheduledTask   -TaskName `"$TaskName`"   # run it now"
Write-Host "  Get-ScheduledTaskInfo -TaskName `"$TaskName`"   # LastRunTime / LastTaskResult (0 = success)"
Write-Host "  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
