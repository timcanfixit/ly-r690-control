<#
.SYNOPSIS
    One-command deploy to the Raspberry Pi: test locally, copy, test on the Pi, restart the web
    service WITHOUT needing the Pi's sudo password, and verify it came back healthy.

.DESCRIPTION
    1. Runs the full local pytest suite - stops here if anything fails (nothing is copied).
    2. Copies pc-app/src (*.py, templates, static) and pc-app/tests to the Pi with scp.
    3. Runs the full pytest suite ON the Pi - stops here if anything fails (files are copied but the
       service keeps running the old code).
    4. Restarts the service by calling POST /api/admin/restart on the Pi (the service ends itself with
       a nonzero exit status and systemd's Restart=on-failure starts it again - no sudo involved).
       The server itself refuses the restart during a run, cooldown, self-tune, unacknowledged fault,
       an in-flight machine write, or while a run is being logged; this script then stops and shows
       the reason rather than forcing it.
    5. Waits for a NEW process to be serving again and reports the machine state.

    One-time bootstrap: a service started BEFORE /api/admin/restart existed answers 404 to it. In that
    case only, and only if the machine state is idle/disconnected/unknown, the script falls back to
    ending the old process directly (kill -9 as the same non-root user the service runs as) so
    systemd restarts it. Every deploy after that first one uses the endpoint.

.PARAMETER SkipTests
    Skip the local test run (the Pi-side run still happens).
.PARAMETER SkipRestart
    Copy and test only; leave the running service on its old code.

.EXAMPLE
    .\tools\deploy-to-pi.ps1
    .\tools\deploy-to-pi.ps1 -SkipRestart
#>
param(
    [string]$PiHost = "ly-r690.local",
    [string]$PiDir = "/home/pi/ly-r690/pc-app",
    [string]$Service = "ly-r690-web",
    [int]$Port = 8080,
    [switch]$SkipTests,
    [switch]$SkipRestart
)

$appRoot = Join-Path (Split-Path -Parent $PSScriptRoot) "pc-app"
$base = "http://127.0.0.1:$Port"
$script:RemoteExit = 0

function Step([string]$msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Stop-Deploy([string]$msg) { Write-Host ""; Write-Host "STOPPED: $msg" -ForegroundColor Red; exit 1 }

# Runs a command on the Pi over ssh and returns its output lines; the exit status lands in $RemoteExit.
function Remote([string]$cmd) {
    $out = & ssh -o ConnectTimeout=10 $PiHost $cmd
    $script:RemoteExit = $LASTEXITCODE
    return $out
}

# Copies every file matching $filter in one local folder to one remote folder. Uses bare file names from
# inside the folder so a Windows drive letter ("F:") is never mistaken by scp for a remote host name.
function Copy-Dir([string]$localSub, [string]$filter, [string]$remoteSub) {
    Push-Location (Join-Path $appRoot $localSub)
    try {
        $names = @(Get-ChildItem -File -Filter $filter | ForEach-Object { $_.Name })
        if ($names.Count -eq 0) { return }
        & scp -q -o ConnectTimeout=10 @names "${PiHost}:${PiDir}/${remoteSub}/"
        if ($LASTEXITCODE -ne 0) { Stop-Deploy "scp failed while copying $localSub" }
        Write-Host ("  {0,-14} {1} file(s)" -f $localSub, $names.Count)
    } finally {
        Pop-Location
    }
}

if (-not $SkipTests) {
    Step "Local tests"
    Push-Location $appRoot
    & py -3 -m pytest tests -q
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Stop-Deploy "local tests failed - nothing was copied" }
}

Step "Copying to $PiHost"
Copy-Dir "src" "*.py" "src"
Copy-Dir "src\templates" "*" "src/templates"
Copy-Dir "src\static" "*" "src/static"
Copy-Dir "tests" "*.py" "tests"

Step "Tests on the Pi"
$out = Remote ('set -o pipefail; cd ' + $PiDir + ' && venv/bin/python -m pytest tests -q 2>&1 | tail -4')
$out | ForEach-Object { Write-Host "  $_" }
if ($RemoteExit -ne 0) { Stop-Deploy "tests failed on the Pi - files are copied but the service was NOT restarted" }

if ($SkipRestart) {
    Step "Skipping restart (-SkipRestart) - the service is still running its old code"
    exit 0
}

Step "Restarting $Service (no sudo)"
$oldPid = Remote "systemctl show $Service -p MainPID --value" | Select-Object -First 1
Write-Host "  current PID: $oldPid"
if ($oldPid -notmatch '^\d+$' -or $oldPid -eq '0') { Stop-Deploy "could not read the service's current PID (is it running?)" }

# The JSON body {"confirm":true} is sent as base64 so no double quotes have to survive PowerShell 5.1's
# native-argument quoting on the way through ssh.
$cmd = "echo eyJjb25maXJtIjp0cnVlfQ== | base64 -d | curl -s -m 15 -w '\n%{http_code}' -X POST -H 'Content-Type: application/json' --data-binary @- $base/api/admin/restart"
$resp = @(Remote $cmd)
$http = ($resp | Select-Object -Last 1)
$body = if ($resp.Count -gt 1) { ($resp[0..($resp.Count - 2)] -join ' ') } else { "" }

if ($http -eq "200") {
    Write-Host "  restart accepted: $body"
} elseif ($http -eq "404") {
    Write-Host "  The running version predates /api/admin/restart (HTTP 404) - using the one-time fallback." -ForegroundColor Yellow
    $state = Remote "curl -s -m 8 $base/api/state" | Out-String
    if ($state -notmatch 'machine_state\W+(idle|disconnected|unknown)\b') {
        Stop-Deploy "machine is not idle/disconnected/unknown ($state) - refusing to end the service. Files are copied; restart it later."
    }
    Remote "kill -9 $oldPid" | Out-Null
    Write-Host "  ended old process $oldPid; systemd restarts it after RestartSec."
} else {
    Stop-Deploy "the service refused the restart (HTTP $http): $body`nFiles are copied; the service is still running the old code."
}

Step "Waiting for the service to come back"
$deadline = (Get-Date).AddSeconds(90)
$newPid = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $p = Remote "systemctl show $Service -p MainPID --value" | Select-Object -First 1
    if ($p -match '^\d+$' -and $p -ne '0' -and $p -ne $oldPid) {
        $code = Remote "curl -s -m 5 -o /dev/null -w '%{http_code}' $base/api/state" | Select-Object -First 1
        if ($code -eq "200") { $newPid = $p; break }
    }
}
if (-not $newPid) { Stop-Deploy "the service did not come back healthy within 90 seconds - check: ssh $PiHost journalctl -u $Service -n 50" }

Step "Verifying"
$state = Remote "curl -s -m 8 $base/api/state" | Out-String
$tracebacks = Remote "journalctl -u $Service --since '1 minute ago' --no-pager 2>/dev/null | grep -ci traceback" | Select-Object -First 1
Write-Host "  new PID $newPid (was $oldPid)"
Write-Host "  state: $($state.Trim())"
Write-Host "  tracebacks in the last minute of the log: $tracebacks"
Write-Host ""
Write-Host "Deploy complete." -ForegroundColor Green
# Explicit: the last native command above (grep -c) exits 1 when it counts zero matches, which is the
# GOOD outcome here and must not leak out as this script's own exit status.
exit 0
