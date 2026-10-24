#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# uv is the only package manager (AGENTS.md §2.1). pip is forbidden. This
# script bootstraps the venv from pyproject.toml + uv.lock so a fresh clone
# reproduces the exact dependency set.
$env:PYTHONUTF8 = 1
$env:PYTHONIOENCODING = 'utf-8'
$env:TEMP = Join-Path (Join-Path $env:USERPROFILE '.water-group') 'tmp'
$env:TMP = $env:TEMP

# Scratch dirs the runtime writes to. Everything lives under ~/.water-group so the
# repo stays code-only (see water-group/daemon/wg_paths.py).
$wg = Join-Path $env:USERPROFILE '.water-group'
New-Item -ItemType Directory -Force $wg, (Join-Path $wg 'state'), (Join-Path $wg 'logs'), (Join-Path $wg 'data'), (Join-Path $wg 'tmp'), .tmp, imports | Out-Null

# uv must be on PATH. The official installer is
#   powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv not found. Install: powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"'
}

# Requires Python 3.13 (pyproject.toml pins requires-python). uv fetches the
# standalone build if the system interpreter is the wrong version.
uv sync --python 3.13
if ($LASTEXITCODE -ne 0) { throw 'uv sync failed' }

# OneBot config lives in water-group/config.json (gitignored). The example is
# the template; copy it and fill onebot_http_url / onebot_token / self_account /
# self_names / groups before starting the stack.
if (-not (Test-Path -LiteralPath 'water-group\config.json')) {
    Copy-Item 'water-group\config.example.json' 'water-group\config.json'
    Write-Output 'Created water-group\config.json from the example. Fill onebot_http_url / onebot_token / self_account / groups before starting.'
}

# .env holds the OneBot send + cloud-voice credentials (gitignored). Only two
# subsystems read keys from here: the reply sender (REPLY_ONEBOT_*) and cloud
# voice ASR (VOICE_CF_*). onebot_http_url / onebot_token can be copied from
# config.json; the WebSocket URL/token/mode have no config.json key, fill them
# by hand once your OneBot exposes forward WebSocket.
if (-not (Test-Path -LiteralPath '.env')) {
    @(
        '# OneBot HTTP for approved QQ sending (reply.py). Localhost only.'
        '# Empty URL disables sending. Can mirror config.json onebot_http_url/onebot_token.'
        "REPLY_ONEBOT_URL='http://127.0.0.1:43000'"
        'REPLY_ONEBOT_TOKEN=YOUR_ONEBOT_TOKEN_HERE'
        '# OneBot forward WebSocket event server, used only by MCP continuous group chat.'
        '# Blank WS token falls back to the HTTP token.'
        "REPLY_ONEBOT_WS_URL='ws://127.0.0.1:43001'"
        'REPLY_ONEBOT_WS_TOKEN=YOUR_ONEBOT_TOKEN_HERE'
        "REPLY_ONEBOT_WS_TOKEN_MODE='independent'"
        ''
        '# Voice ASR runs on a Cloudflare Worker. The bearer token stays here.'
        'VOICE_CF_URL='
        'VOICE_CF_TOKEN=YOUR_VOICE_TOKEN_HERE'
        'VOICE_CF_MODEL=whisper-large-v3-turbo'
        'VOICE_LANGUAGE=zh'
        'VOICE_MAX_SECONDS=180'
        'VOICE_MAX_BYTES=16777216'
    ) -join "`r`n" | Set-Content -LiteralPath '.env' -Encoding utf8
    Write-Output 'Created .env from the template. Fill REPLY_ONEBOT_* and VOICE_CF_* (both gitignored, never committed).'
}

# Import smoke only. bootstrap.py --status-only needs NapCat already running and
# QQ logged in, which a fresh clone does not have at install time, so this stage
# stops at confirming the keep modules import cleanly.
& .\.venv\Scripts\python.exe -c "import chatlocal.mcp_server, chatlocal.mcp_chat_media, chatlocal.voice; print('import ok')"
if ($LASTEXITCODE -ne 0) { throw 'import smoke failed' }

Write-Output 'Ready. Fill water-group\config.json and .env, then double-click water-group\start-bot.pyw'
