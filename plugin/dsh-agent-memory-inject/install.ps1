# Install the agent-memory inject plugin into the DSH desktop profile (ASCII only on purpose)
#
# WHY this is a separate, opt-in script
#   The plugin hooks `agent/pre-step` - a waterfall whose arity is load-bearing. A wrong
#   hook breaks EVERY tool call in the host (observed live once by the runaway-guard author).
#   So: install copies files only, prints the YAML to review, and lets the human enable it.
#   Nothing here edits cordis.patch.yml automatically.
#
# USAGE
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1            # copy files + print YAML
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -Uninstall # remove files
#
# AFTER INSTALL (manual, requires a DSH restart):
#   1. paste the printed YAML block into `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml`
#      (or into the profile's insert block) and adjust pythonPath/cliPath/repoPath;
#   2. restart DSH (plugin entries are NOT hot-reloaded; only MCP entries are);
#   3. verify: open a chat, ask something covered by your cards, and check that the
#      context now carries a `<!-- agent-memory:cards-injected -->` block, or watch the log.
#   4. rollback: `-Uninstall`, remove the YAML block, restart again.

param(
    [switch]$Uninstall,
    [string]$Profile = "desktop"
)

$ErrorActionPreference = 'Stop'

$pluginName = 'dsh-agent-memory-inject'
$home2 = $env:USERPROFILE
$profiles = Join-Path $home2 ".dsh\profiles\$Profile"
$target = Join-Path $profiles "node_modules\@local\$pluginName"
$source = $PSScriptRoot

if ($Uninstall) {
    if (Test-Path $target) {
        Remove-Item $target -Recurse -Force
        Write-Host "removed: $target"
    } else {
        Write-Host "not installed: $target"
    }
    Write-Host "Now remove the agent-memory-inject block from cordis.patch.yml and restart DSH."
    exit 0
}

if (-not (Test-Path $profiles)) { throw "profile not found: $profiles" }
New-Item -ItemType Directory -Force -Path $target | Out-Null

foreach ($item in @('index.js', 'package.json', 'lib', 'test', 'README.md')) {
    $src = Join-Path $source $item
    if (Test-Path $src) { Copy-Item $src $target -Recurse -Force }
}
Write-Host "installed -> $target"

$py = Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$cli = 'F:\agent-memory\cli.py'

Write-Host ''
Write-Host '=== paste this into cordis.patch.yml (plugin entries need a DSH restart) ==='
Write-Host ''
@"
# agent-memory: inject conclusion cards before each request (idempotent + cooldown + fail-soft)
- id: agent-memory-inject
  name: '@local/dsh-agent-memory-inject'
  config:
    pythonPath: '$py'
    cliPath: '$cli'
    repoPath: 'F:\agent-memory'
    maxCards: 5
    maxChars: 900
    cooldownMs: 120000
    timeoutMs: 3000
"@ | Write-Host
Write-Host '=== end ==='
Write-Host ''
Write-Host 'Then restart DSH. Verify with a chat that should hit one of your cards.'
Write-Host 'Rollback: install.ps1 -Uninstall, remove the block above, restart again.'
