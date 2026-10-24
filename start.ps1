#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONUTF8 = 1
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = Join-Path $env:USERPROFILE '.water-group\tmp'
$env:TMP = $env:TEMP

# The water-group backend. Reply.py sends QQ messages over OneBot HTTP directly
# (chatlocal.message_sender.QQSender reads REPLY_ONEBOT_URL/REPLY_ONEBOT_TOKEN
# from .env), so there is no separate reply-sender process to launch. SnowLuma
# was removed in Phase 3; the OneBot 11 gateway (NapCat) must already be
# running with HTTP + forward WebSocket on the ports in water-group/config.json.
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\server_wg.py"
