# Scarica (se manca) Hand Tracking Streamer v1.1.0 e installa sul Meta Quest via adb.
# Requisiti: Quest in Developer mode, USB collegato, "Consenti debug USB" sul visore.
# Uso: dalla root del repo: .\scripts\install_hand_tracking_streamer_quest.ps1

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dir = Join-Path $root "installers\quest"
$apk = Join-Path $dir "hand_tracking_streamer.apk"
$url = "https://github.com/wengmister/hand-tracking-streamer/releases/download/v1.1.0/hand_tracking_streamer.apk"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Find-Adb {
    $candidates = @(
        "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
        "$env:USERPROFILE\AppData\Local\Android\Sdk\platform-tools\adb.exe",
        "C:\Android\platform-tools\adb.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

if (-not (Test-Path -LiteralPath $apk) -or (Get-Item $apk).Length -lt 10MB) {
    Write-Host "Scarico HTS da GitHub (circa 77 MB)..." -ForegroundColor Cyan
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $apk -UseBasicParsing
    Write-Host "OK: $apk" -ForegroundColor Green
} else {
    Write-Host "APK gia presente: $apk" -ForegroundColor Gray
}

$adb = Find-Adb
if (-not $adb) {
    Write-Host ""
    Write-Host "adb non trovato. Installa Android Platform-Tools:" -ForegroundColor Yellow
    Write-Host "  winget install Google.PlatformTools" -ForegroundColor White
    Write-Host "Poi riapri il terminale e rilancia questo script." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "APK salvato qui (installazione manuale da altra macchina):" -ForegroundColor Cyan
    Write-Host "  $apk" -ForegroundColor White
    exit 2
}

Write-Host "adb: $adb" -ForegroundColor Gray
& $adb kill-server 2>$null
Start-Sleep -Milliseconds 500
& $adb start-server
$dev = & $adb devices
Write-Host $dev
if ($dev -notmatch "device`$") {
    Write-Host ""
    Write-Host "Nessun dispositivo autorizzato. Sul Quest accetta 'Debug USB' e riprova." -ForegroundColor Red
    exit 3
}

Write-Host "Installo sul Quest (adb install -r)..." -ForegroundColor Cyan
& $adb install -r $apk
if ($LASTEXITCODE -ne 0) {
    Write-Host "Install fallita (codice $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host "Fatto. Sul Quest cerca l'app nelle app non ufficiali / Unknown sources." -ForegroundColor Green
