# Reload the water-group stack without spawning anything from the agent sandbox.
#
# The CLI only writes a request file under ~/.water-group/state and waits for the
# guard to write a result.  The guard is the only process that restarts children.
# `--wait` defaults to wg_budget.WAIT_BUDGET (90) inside reload.py; pass an
# explicit --wait to override it (issue #230).
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

# 不在这里补 --wait：reload.py 的默认值就是 wg_budget.WAIT_BUDGET，单点定义（issue #230）。
# 用户显式传的 --wait 原样透传；此前无参时补 '120' 与 reload.py 的字面量默认同属超预算，
# 已删。
& $python (Join-Path $PSScriptRoot 'water-group\daemon\reload.py') @ReloadArgs
exit $LASTEXITCODE
