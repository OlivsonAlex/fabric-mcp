<#
    Register-WhenClosed.ps1

    Runs a registration script AFTER Claude Desktop has fully exited, then logs the
    result for reading afterwards.

    Why this exists: Claude Code can run inside Claude Desktop's own process tree
    (powershell <- claude-code <- Claude.exe). In that topology PowerShell works
    fully, but quitting the app from the tray kills the agent mid-install. The
    registration scripts deliberately refuse to write while Claude Desktop is alive,
    so the agent cannot complete the install itself. This decouples the two: the
    agent starts this, the human quits the app, this does the write, the agent reads
    the log when the app is back.

    Launch it DETACHED so it is not killed along with the agent:

        Start-Process -FilePath powershell -WindowStyle Hidden -ArgumentList @(
          '-NoProfile','-ExecutionPolicy','Bypass','-File','.\Register-WhenClosed.ps1',
          '-Script','.\setup.ps1',
          '-Arguments','-RegisterClaude,-Name,fabric-clienta,-Subscription,<guid>')

    Then quit Claude Desktop from the tray. Read _local\register-when-closed.log after.
#>
param(
  [Parameter(Mandatory=$true)][string]$Script,
  [string]$Arguments = "",
  [int]$TimeoutMinutes = 15,
  [string]$LogPath
)

$ErrorActionPreference = 'Stop'
$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }

if (-not $LogPath) {
    $localDir = Join-Path $root "_local"
    if (-not (Test-Path $localDir)) { New-Item -ItemType Directory -Path $localDir -Force | Out-Null }
    $LogPath = Join-Path $localDir "register-when-closed.log"
}

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

Log "=== Register-WhenClosed started"
Log "script    : $Script"
Log "arguments : $Arguments"
Log "timeout   : $TimeoutMinutes minutes"

# Report the process chain, so the log shows whether this was launched from inside
# Claude Desktop's tree - which is the situation this script exists for.
try {
    $me = Get-CimInstance Win32_Process -Filter "ProcessId = $PID"
    $chain = @()
    $cur = $me
    for ($i = 0; $i -lt 6 -and $cur; $i++) {
        $chain += "$($cur.Name)($($cur.ProcessId))"
        if (-not $cur.ParentProcessId) { break }
        $cur = Get-CimInstance Win32_Process -Filter "ProcessId = $($cur.ParentProcessId)" -ErrorAction SilentlyContinue
    }
    Log ("parent chain: " + ($chain -join " <- "))
} catch { Log "parent chain: unavailable ($($_.Exception.Message))" }

$target = Join-Path $root ([System.IO.Path]::GetFileName($Script))
if (-not (Test-Path $target)) { Log "FAIL: script not found: $target"; exit 1 }

Log "waiting for Claude Desktop to exit - quit it from the system tray now"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
$sawItRunning = $false
while ((Get-Date) -lt $deadline) {
    $procs = @(Get-Process -Name Claude -ErrorAction SilentlyContinue)
    if ($procs.Count -gt 0) { $sawItRunning = $true; Start-Sleep -Seconds 2; continue }
    break
}

if (@(Get-Process -Name Claude -ErrorAction SilentlyContinue).Count -gt 0) {
    Log "FAIL: timed out after $TimeoutMinutes minutes - Claude Desktop is still running. Nothing was written."
    exit 1
}
Log ("Claude Desktop is not running" + $(if ($sawItRunning) { " (it exited)" } else { " (it was already closed)" }))

# Let file handles settle before touching the config the app was holding.
Start-Sleep -Seconds 3

$argList = @()
if ($Arguments) { $argList = $Arguments.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ } }
Log ("running: {0} {1}" -f $target, ($argList -join " "))

try {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $target @argList 2>&1
    $code = $LASTEXITCODE
    foreach ($line in $out) { Add-Content -Path $LogPath -Value ("    " + $line) }
    Log "exit code: $code"
    if ($code -eq 0) { Log "=== done. Start Claude Desktop, then verify with fabric_whoami." }
    else { Log "=== the registration script reported a failure - see the output above." }
    exit $code
} catch {
    Log "FAIL: $($_.Exception.Message)"
    exit 1
}
