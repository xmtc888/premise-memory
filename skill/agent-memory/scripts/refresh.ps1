# agent-memory daily refresh (ASCII only on purpose)
#
# Runs: ingest -> topics --build -> regenerate each project's .agent-memory\context.md
#
# WHY ASCII: PowerShell 5.1 decodes BOM-less .ps1 as ANSI(GBK); Chinese in this file
# would turn into mojibake and break parsing. Chinese lives in targets.json (read as UTF-8).
#
# Manual run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File <skill>\scripts\refresh.ps1
#
# Register (daily 06:30) -- NOT done automatically; review then run:
#   schtasks /Create /TN "agent-memory-daily" /SC DAILY /ST 06:30 ^
#       /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<skill>\scripts\refresh.ps1\"" /F

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_resolve.ps1')

$amHome = Resolve-AmHome
$py  = Resolve-AmPython $amHome
$cli = Join-Path $amHome 'cli.py'
$log = Join-Path $amHome 'index\daily-refresh.log'
# targets live in the DATA dir (per-machine), NOT in the skill dir:
#   <home>\index\targets.json
# Rationale: which projects exist differs per machine; the skill must stay generic.
$targetsFile = Join-Path $amHome 'index\targets.json'

if (-not (Test-Path $py))  { throw "interpreter not found: $py" }
if (-not (Test-Path $cli)) { throw "cli.py not found: $cli" }

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    $dir = Split-Path $log -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Host $line
}

# read targets as UTF-8 (do not use Get-Content default encoding here)
$targets = @()
if (Test-Path $targetsFile) {
    $json = [System.IO.File]::ReadAllText($targetsFile, [System.Text.Encoding]::UTF8)
    $targets = ($json | ConvertFrom-Json).targets
} else {
    Write-Log "WARN: targets.json not found, only ingest+topics will run"
}

# Duplicate-run guard (2026-11-17): the task now has BOTH a daily 06:30 trigger
# (StartWhenAvailable = missed-run catch-up at boot) and an AtLogOn trigger,
# so two full runs on the same day are normal. ingest is idempotent, but a full
# run costs ~150s CPU -- skip it if the last one finished recently.
# The "=== done" marker is pure ASCII, so Select-String works whatever encoding
# the log bytes end up in.
$skipHours = 20
if (Test-Path $log) {
    $lastDone = Select-String -Path $log -Pattern '=== done in' | Select-Object -Last 1
    if ($lastDone) {
        $parts = $lastDone.Line -split '\s+'
        try {
            $ts = [datetime]::ParseExact(($parts[0] + ' ' + $parts[1]),
                'yyyy-MM-dd HH:mm:ss', [System.Globalization.CultureInfo]::InvariantCulture)
            $age = ((Get-Date) - $ts).TotalHours
            if ($age -lt $skipHours) {
                Write-Log ("skip: last full run finished {0:N1}h ago (threshold {1}h)" -f $age, $skipHours)
                exit 0
            }
        } catch {
            # unparseable timestamp -> fall through and run
        }
    }
}

Write-Log "=== start ==="
$t0 = Get-Date

# Pre-check (2026-11-09): report whether the EXISTING injection blocks are already stale.
# Reason: the whole point of this task is that nobody notices when it stops running --
# a downstream project's context.md sat 5.9 days out of date before anyone looked. Logging it at the
# START means a broken schedule shows up as "WARN ... stale before this run" in the log.
$eap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$chkOut = & $py $cli inject --check 2>&1
$chkRc = $LASTEXITCODE
$ErrorActionPreference = $eap
if ($chkRc -ne 0) {
    $firstLine = ($chkOut | Where-Object { $_ -match 'STALE' } | Select-Object -First 1)
    if (-not $firstLine) { $firstLine = ($chkOut | Select-Object -First 1) }
    Write-Log ("WARN: injection block stale before this run -> " + $firstLine)
} else {
    Write-Log "pre-check: injection blocks fresh"
}

try {
    & $py $cli ingest         | Out-Null ; Write-Log "ingest ok"
    & $py $cli topics --build | Out-Null ; Write-Log "topics ok"
    # procedure cache (2026-11-17): MCP tools and inject --procedures read it.
    # Mining takes ~12s (mine_repairs 9.1s + mine_rituals 2.7s), so precompute it here
    # once a day; tool calls then read the cache in milliseconds. Stale cache is reported
    # explicitly by the reader (never silently used as fresh data).
    & $py $cli procedure --refresh-cache | Out-Null ; Write-Log "procedure cache ok"
} catch {
    Write-Log ("**ingest/topics FAILED**: " + $_.Exception.Message)
    exit 1
}

# Machine-state cards (2026-11-18): declarative live checks, never shell commands.
# A failing check means "this machine is no longer in that state" => the card is stale.
# Log it, but do NOT abort: a stale fact must not block ingest/inject.
$mcOut = & $py $cli cards validate --machine 2>&1
if ($LASTEXITCODE -ne 0) {
    $mcLast = ($mcOut | Select-Object -Last 1)
    Write-Log ("**cards machine-check FAILED**: " + $mcLast)
} else {
    Write-Log "cards machine-check ok"
}

foreach ($t in $targets) {
    $outDir = Split-Path $t.out -Parent
    if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
    # --procedures (2026-11-17): inject this project's bounded procedure list
    # (top 3, 500 chars, premise-checked). Without it the block never reaches agents.
    & $py $cli inject $t.entity --procedures --write $t.out | Out-Null
    if (Test-Path $t.out) {
        $size = (Get-Item $t.out).Length
        Write-Log ("inject ok: {0} -> {1} ({2} bytes)" -f $t.entity, $t.out, $size)
    } else {
        Write-Log ("**inject FAILED**: " + $t.out)
    }
}
Write-Log ("=== done in {0:N0}s ===" -f ((Get-Date) - $t0).TotalSeconds)
