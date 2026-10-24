# Reload the water-group stack without spawning anything from the agent sandbox.
#
# The CLI only writes a request file under ~/.water-group/state and waits for the
# guard to write a result.  The guard is the only process that restarts children.
# `--wait` defaults to 120 seconds; pass an explicit --wait to override it.
#
#   .\reload.ps1
#   .\reload.ps1 --only commands
#   .\reload.ps1 --watch
#   .\reload.ps1 --status
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ReloadArgs
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$cliArgs = @($ReloadArgs)
$hasWait = $false
foreach ($arg in $cliArgs) {
    if ($arg -eq '--wait' -or $arg -like '--wait=*') { $hasWait = $true }
}
if (-not $hasWait) {
    $cliArgs += @('--wait', '120')
}

& $python (Join-Path $PSScriptRoot 'water-group\daemon\reload.py') @cliArgs
exit $LASTEXITCODE
