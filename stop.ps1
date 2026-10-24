# Stop the whole water-group stack.
#
# This used to match only `server_wg.py`, so the supervisor put the backend right
# back up and an injected QQ stayed online: a half stop, which looks worse than no
# stop at all. The rules now live in one place -- water-group/daemon/up.py
# stop_stack() -- and this script only invokes them, so `.\stop.ps1`, `up.py --stop`
# and a double-clicked `water-group/stop-bot.pyw` can never disagree.
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }
& $python (Join-Path $PSScriptRoot 'water-group\daemon\up.py') --stop
exit $LASTEXITCODE
