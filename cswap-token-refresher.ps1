#Requires -Version 5.1
<#
.SYNOPSIS
    Cycle through every cswap-managed Claude profile and exercise its OAuth tokens.

.DESCRIPTION
    For each profile: switch to it, inspect Claude's .credentials.json, and if the
    refresh token expires within -WithinDays days, make a tiny `claude` API call.
    That call prompts a token refresh only when the access token is already
    expired, so this exercises the refresh token but cannot force a rotation on
    its own. The originally-active profile is always restored, including on error
    or Ctrl-C.

    Exits 0 only if every profile succeeded and the original profile was restored;
    otherwise 1.
#>

[CmdletBinding()]
param(
    [int]$WithinDays = 4,
    [string]$Prompt = "Reply exactly: Claude Code is OK",
    [int]$TimeoutSeconds = 30,

    # If set, tee this run's console output to a dated file in this directory
    # (created if needed) and delete files older than -LogRetentionDays.
    [string]$LogDir,
    [int]$LogRetentionDays = 14
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Optional run log ---------------------------------------------------------
$script:TranscriptOn = $false

function Stop-Log {
    if ($script:TranscriptOn) {
        try { Stop-Transcript | Out-Null } catch {}
        $script:TranscriptOn = $false
    }
}

if ($LogDir) {
    try {
        New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction Stop | Out-Null

        if ($LogRetentionDays -gt 0) {
            $cutoff = (Get-Date).AddDays(-$LogRetentionDays)
            Get-ChildItem -LiteralPath $LogDir -Filter 'cswap-refresher_*.log' -File -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -lt $cutoff } |
                Remove-Item -Force -ErrorAction SilentlyContinue
        }

        $logFile = Join-Path $LogDir ("cswap-refresher_{0:yyyy-MM-dd_HHmmss}.log" -f (Get-Date))
        Start-Transcript -LiteralPath $logFile -Force | Out-Null
        $script:TranscriptOn = $true
    }
    catch {
        Write-Warning "Could not start log in '$LogDir': $($_.Exception.Message)"
    }
}

function Get-CredentialsPath {
    # $env:USERPROFILE is set on Windows only; fall back to $HOME elsewhere.
    $base = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    return (Join-Path $base ".claude/.credentials.json")
}

# Number -> email, populated from `cswap list --json`. Used only for display.
$script:EmailByNumber = @{}

function Format-Profile {
    param($Number)
    $key = [string]$Number
    if ($key -and $script:EmailByNumber.Contains($key)) {
        return "$Number ($($script:EmailByNumber[$key]))"
    }
    return "$Number"
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Arguments,
        [int]$TimeoutSeconds = 0,
        [switch]$NoEcho
    )
    if (-not $NoEcho) { Write-Host "==> $Exe $($Arguments -join ' ')" }

    $outFile = [System.IO.Path]::GetTempFileName()
    $errFile = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $Exe -ArgumentList $Arguments -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile

        # Windows PowerShell 5.1 leaves $proc.ExitCode $null unless the process
        # handle was touched before the process exits. Cache it now.
        $null = $proc.Handle

        if ($TimeoutSeconds -gt 0) {
            if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
                try { $proc.Kill() } catch {}
                $proc.WaitForExit()
                return [pscustomobject]@{ ExitCode = 124; StdOut = ""; StdErr = "timed out after ${TimeoutSeconds}s" }
            }
        }
        else {
            $proc.WaitForExit()
        }

        $out = Get-Content -LiteralPath $outFile -Raw -ErrorAction SilentlyContinue
        $err = Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue
        return [pscustomobject]@{
            ExitCode = $proc.ExitCode
            StdOut   = if ($null -eq $out) { "" } else { $out }
            StdErr   = if ($null -eq $err) { "" } else { $err }
        }
    }
    finally {
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-RefreshExpiry {
    # Returns a [datetime] (UTC) or $null.
    $path = Get-CredentialsPath
    if (-not (Test-Path -LiteralPath $path)) {
        throw "credentials file not found: $path"
    }
    $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $oauth = $data.claudeAiOauth
    if ($null -eq $oauth) { return $null }
    $val = $oauth.refreshTokenExpiresAt
    if ($null -eq $val) { return $null }

    if ($val -is [string]) {
        if ([string]::IsNullOrWhiteSpace($val)) { return $null }
        try {
            return ([datetimeoffset]::Parse($val, $null, [System.Globalization.DateTimeStyles]::AssumeUniversal)).UtcDateTime
        }
        catch { return $null }
    }

    # Numeric: milliseconds vs seconds since epoch.
    $num = [double]$val
    $seconds = if ($num -gt 1e12) { $num / 1000.0 } else { $num }
    return [System.DateTimeOffset]::FromUnixTimeMilliseconds([long]([math]::Round($seconds * 1000))).UtcDateTime
}

# Make a minimal `claude` API call, which prompts a token refresh *if* the
# access token is expired. Returns $true if the call itself succeeded.
function Invoke-Claude {
    $res = Invoke-Native -Exe "claude" -Arguments @("-p", "`"$Prompt`"") -TimeoutSeconds $TimeoutSeconds
    if ($res.StdOut.Trim()) { Write-Host $res.StdOut.Trim() }
    if ($res.ExitCode -ne 0) {
        if ($res.StdErr.Trim()) { Write-Warning $res.StdErr.Trim() }
        return $false
    }
    return $true
}

function Invoke-Profile {
    param([Parameter(Mandatory)][int]$Number)

    Write-Host "==> cswap switch $(Format-Profile $Number)"
    $switch = Invoke-Native -Exe "cswap" -Arguments @("switch", "$Number") -NoEcho
    if ($switch.ExitCode -ne 0) {
        $msg = if ($switch.StdErr.Trim()) { $switch.StdErr.Trim() } else { $switch.StdOut.Trim() }
        return [pscustomobject]@{ Success = $false; Message = "cswap switch failed: $msg" }
    }

    try {
        $expiry = Get-RefreshExpiry
    }
    catch {
        return [pscustomobject]@{ Success = $false; Message = "could not read credentials: $($_.Exception.Message)" }
    }

    if ($null -eq $expiry) {
        return [pscustomobject]@{ Success = $false; Message = "refreshTokenExpiresAt missing or unparseable" }
    }

    $daysLeft = ($expiry - [datetime]::UtcNow).TotalDays

    if ($daysLeft -le 0) {
        return [pscustomobject]@{ Success = $false; Message = ("refresh token already expired ({0:o})" -f $expiry) }
    }

    if ($daysLeft -gt $WithinDays) {
        return [pscustomobject]@{ Success = $true; Message = ("OK (fresh, {0:N1}d left)" -f $daysLeft) }
    }

    if (-not (Invoke-Claude)) {
        return [pscustomobject]@{ Success = $false; Message = "``claude`` call failed" }
    }

    # Confirm by re-reading the credentials file. A plain `claude` call only
    # rotates the refresh token when the access token was actually expired, so
    # refreshTokenExpiresAt moving forward is a positive "it refreshed" signal;
    # it staying put just means no refresh was due (access token still valid) --
    # not a failure.
    try {
        $newExpiry = Get-RefreshExpiry
    }
    catch {
        return [pscustomobject]@{ Success = $false; Message = "could not re-read credentials after refresh: $($_.Exception.Message)" }
    }

    if ($null -eq $newExpiry) {
        return [pscustomobject]@{ Success = $false; Message = "refreshTokenExpiresAt missing after refresh" }
    }

    $advanceSeconds = ($newExpiry - $expiry).TotalSeconds
    if ($advanceSeconds -le 1) {
        return [pscustomobject]@{
            Success = $true
            Message = ("OK (no refresh needed, refreshTokenExpiresAt unchanged at {0:o}, {1:N1}d left)" -f $newExpiry, $daysLeft)
        }
    }

    $newDaysLeft = ($newExpiry - [datetime]::UtcNow).TotalDays
    return [pscustomobject]@{
        Success = $true
        Message = ("OK (refreshed, was {0:N1}d left, now {1:N1}d left, +{2:N1}d)" -f $daysLeft, $newDaysLeft, ($advanceSeconds / 86400))
    }
}

# ---------------------------------------------------------------------------

$exitCode = 1
$original = $null
$results = [ordered]@{}
$numbers = @()

try {
    $listRes = Invoke-Native -Exe "cswap" -Arguments @("list", "--json")
    if ($listRes.ExitCode -ne 0) {
        throw "``cswap list --json`` failed (exit $($listRes.ExitCode)): $($listRes.StdErr.Trim())"
    }
    $listing = $listRes.StdOut | ConvertFrom-Json

    $numbers = @($listing.accounts | ForEach-Object { [int]$_.number })
    if ($numbers.Count -eq 0) {
        throw "no profiles found in ``cswap list --json``"
    }

    foreach ($acct in $listing.accounts) {
        $email = if ($acct.PSObject.Properties['email']) { $acct.email } else { $null }
        if ($email) { $script:EmailByNumber[[string][int]$acct.number] = [string]$email }
    }

    $original = $listing.activeAccountNumber
    if ($null -eq $original) {
        $active = $listing.accounts | Where-Object { $_.active } | Select-Object -First 1
        if ($active) { $original = [int]$active.number }
    }

    Write-Host ("Found {0} profile(s): {1}" -f $numbers.Count, (($numbers | ForEach-Object { Format-Profile $_ }) -join ", "))
    Write-Host "Original active profile: $(Format-Profile $original)"
    Write-Host ""
}
catch {
    Write-Error $_.Exception.Message
    Stop-Log
    exit 1
}

$restoreMsg = "not attempted"
$restoreOk = $false

try {
    foreach ($n in $numbers) {
        $r = Invoke-Profile -Number $n
        $results[[string]$n] = $r
        Write-Host "Profile ${n}: $($r.Message)"
        Write-Host ""
    }
}
finally {
    if ($null -ne $original) {
        Write-Host "==> restoring original active profile: cswap switch $(Format-Profile $original)"
        $restore = Invoke-Native -Exe "cswap" -Arguments @("switch", "$original") -NoEcho
        if ($restore.ExitCode -eq 0) {
            $restoreOk = $true
            $restoreMsg = "OK"
        }
        else {
            $restoreMsg = if ($restore.StdErr.Trim()) { $restore.StdErr.Trim() } elseif ($restore.StdOut.Trim()) { $restore.StdOut.Trim() } else { "failed" }
        }
    }
    else {
        $restoreMsg = "unknown original profile"
    }

    Write-Host ""
    Write-Host ("{0} Summary {0}" -f ("=" * 20))
    $allOk = $true
    foreach ($n in $numbers) {
        $key = [string]$n
        $emailSuffix = if ($script:EmailByNumber.Contains($key)) { ", $($script:EmailByNumber[$key])" } else { "" }
        if ($results.Contains($key)) {
            $r = $results[$key]
            Write-Host ("  Profile {0}: {1}{2}" -f $n, $r.Message, $emailSuffix)
            if (-not $r.Success) { $allOk = $false }
        }
        else {
            Write-Host ("  Profile {0}: not processed{1}" -f $n, $emailSuffix)
            $allOk = $false
        }
    }
    Write-Host ("  restore -> Profile {0}: {1}" -f (Format-Profile $original), $restoreMsg)

    if ($allOk -and $restoreOk -and $results.Count -eq $numbers.Count) {
        $exitCode = 0
    }
}

Stop-Log
exit $exitCode
