#Requires -Version 5.1
# Fetch a pinned NapCat Shell release into tools/napcat-shell/.
#
# tools/napcat-shell/ is gitignored (the bundle is ~36MB; the slim repo ships
# the fetcher, not the bytes). QQ itself and a matching QQNT build must already
# be installed on the host — NapCat Shell is the OneBot 11 adapter that hooks
# into the running QQ, not a standalone QQ.
#
# Pin: the version + sha256 below. Re-pin by running this script with -Update
# (it fetches the latest release, writes the new version + hash to this file's
# $NapCatVersion / $NapCatSha256, and exits so a human can review the diff).
#
# Usage:
#   .\scripts\setup-napcat.ps1            # install the pinned version
#   .\scripts\setup-napcat.ps1 -Update    # fetch latest, re-pin version+hash
param([switch]$Update)

$ErrorActionPreference = 'Stop'
$repo = 'NapNeko/NapCatQQ'
$asset = 'NapCat.Shell.zip'  # Shell layout: framework+shell, hooks the host QQ.
$projectRoot = Split-Path -Parent $PSScriptRoot
$destDir = Join-Path (Join-Path $projectRoot 'tools') 'napcat-shell'

# Pinned release (re-pinned via -Update). sha256 is of the release .zip asset.
$NapCatVersion = 'v4.18.41'
$NapCatSha256 = '6801f93e533846cea05f4f531874a0e84371ab2331c5585b57cd7e3b869cf96b'

function Get-LatestNapCat {
    param([string]$Repository, [string]$Asset)
    # Use gh if authenticated (higher rate limit); fall back to the public API.
    $tag = $null; $url = $null
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        $json = gh release view --repo $Repository --json tagName,assets 2>$null | ConvertFrom-Json
        if ($json -and $json.tagName) {
            $tag = $json.tagName
            $url = ($json.assets | Where-Object { $_.name -eq $Asset }).url
        }
    }
    if (-not $tag) {
        $r = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/latest" -Headers @{ 'User-Agent' = 'tulpa-setup' }
        $tag = $r.tag_name
        $url = ($r.assets | Where-Object { $_.name -eq $Asset }).browser_download_url
    }
    return $tag, $url
}

if ($Update) {
    $tag, $url = Get-LatestNapCat -Repository $repo -Asset $asset
    if (-not $url) { throw "could not resolve latest NapCat $asset asset" }
    $tmp = Join-Path $env:TEMP "napcat-$tag.zip"
    Invoke-WebRequest -Uri $url -OutFile $tmp
    $hash = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Write-Output "Latest: $tag"
    Write-Output "sha256: $hash"
    Write-Output "URL:    $url"
    Write-Output "Re-pin: edit `$NapCatVersion / `$NapCatSha256 at the top of $PSCommandPath to these values, then run again without -Update."
    return
}

$dlUrl = "https://github.com/$repo/releases/download/$NapCatVersion/$asset"
if (Test-Path -LiteralPath $destDir) {
    Write-Output "tools\napcat-shell already exists; remove it to re-fetch."
    return
}
New-Item -ItemType Directory -Force -Path $destDir | Out-Null
$tmp = Join-Path $env:TEMP "napcat-$NapCatVersion.zip"
Write-Output "Downloading NapCat $NapCatVersion ($asset)..."
Invoke-WebRequest -Uri $dlUrl -OutFile $tmp
$actual = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $NapCatSha256) {
    Remove-Item $tmp -ErrorAction SilentlyContinue
    throw "sha256 mismatch: expected $NapCatSha256, got $actual. Re-pin with -Update or verify the asset."
}
Write-Output "sha256 ok ($actual)"
Expand-Archive -LiteralPath $tmp -DestinationPath $destDir -Force
Remove-Item $tmp -ErrorAction SilentlyContinue
Write-Output "NapCat $NapCatVersion installed to tools\napcat-shell."
Write-Output "Next: install QQNT on the host, then run launcher.bat in tools\napcat-shell (admin) and log in."
