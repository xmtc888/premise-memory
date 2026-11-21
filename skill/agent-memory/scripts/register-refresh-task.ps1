# agent-memory: register the daily refresh task (ASCII only on purpose)
#
# WHY this script exists
#   scripts\refresh.ps1 already does ingest -> topics -> inject for every target,
#   but NOTHING was scheduling it: on 2026-11-09 the log showed the last run at
#   2026-11-03 and a downstream project's .agent-memory\context.md was 5.9 days stale, while
#   agents kept reading it as "current" facts. A refresh job that nobody schedules
#   is a job that silently stops working.
#
# WHY ASCII: PowerShell 5.1 decodes BOM-less .ps1 as ANSI(GBK); Chinese in this file
# would turn into mojibake and break parsing.
#
# USAGE
#   powershell -NoProfile -ExecutionPolicy Bypass -File <skill>\scripts\register-refresh-task.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File <skill>\scripts\register-refresh-task.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File <skill>\scripts\register-refresh-task.ps1 -Remove
#
# NOTES
#   - Creates a per-user task (no admin needed, no stored password) running DAILY at 06:30.
#   - /IT = run only when the user is logged on. That is deliberate: ingest needs the
#     user's own session files (agent histories live under the user profile).
#   - Re-running is safe: /F overwrites an existing task with the same name.

param(
    [switch]$Status,
    [switch]$Remove,
    [string]$At = '06:30'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_resolve.ps1')

$taskName = 'agent-memory-daily'
$amHome = Resolve-AmHome
$refresh = Join-Path $PSScriptRoot 'refresh.ps1'

if ($Remove) {
    schtasks /Delete /TN $taskName /F
    Write-Host ("removed task: " + $taskName)
    exit 0
}

if ($Status) {
    $q = schtasks /Query /TN $taskName /FO LIST /V 2>&1 | Out-String
    if ($q -match $taskName) {
        Write-Host ("task exists: " + $taskName)
        ($q -split "`r?`n") | Where-Object {
            $_ -match 'Next Run|Last Run|Last Result|Status|Task To Run'
        } | ForEach-Object { Write-Host ("  " + $_.Trim()) }
        exit 0
    }
    Write-Host ("task NOT found: " + $taskName)
    exit 1
}

if (-not (Test-Path $refresh)) { throw ("refresh.ps1 not found: " + $refresh) }

# Build the command line. Quote the script path because skill folders may contain spaces.
$tr = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $refresh + '"'
$args = @('/Create', '/TN', $taskName, '/SC', 'DAILY', '/ST', $At, '/TR', $tr, '/IT', '/F')
$out = & schtasks @args 2>&1
$rc = $LASTEXITCODE
$out | ForEach-Object { Write-Host $_ }
if ($rc -ne 0) {
    Write-Host ("FAILED to register task (exit " + $rc + ")")
    exit $rc
}

Write-Host ("registered: " + $taskName + " daily at " + $At)
Write-Host ("  runs: " + $tr)
Write-Host ("  home: " + $amHome)
Write-Host ""
Write-Host "verify with:  powershell -NoProfile -ExecutionPolicy Bypass -File <this script> -Status"
Write-Host "run once now: powershell -NoProfile -ExecutionPolicy Bypass -File <refresh.ps1>"
