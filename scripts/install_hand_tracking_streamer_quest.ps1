# Hand Tracking Streamer v1.1.0 -> Meta Quest via adb (fonte ufficiale GitHub).
# Requisiti: Developer mode, USB, sul visore accetta "Consenti debug USB" / autorizza PC.
# Uso (da root repo): .\scripts\install_hand_tracking_streamer_quest.ps1

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dir = Join-Path $root "installers\quest"
$apk = Join-Path $dir "hand_tracking_streamer.apk"
$url = "https://github.com/wengmister/hand-tracking-streamer/releases/download/v1.1.0/hand_tracking_streamer.apk"
# Digest ufficiale dall'API GitHub (asset hand_tracking_streamer.apk, release v1.1.0)
$expectedSha256 = "E0B41AB36E0BCBBC1A1D616BD659BAD6D93C02EFCB332C1D87C2BEE1C6E04879"

New-Item -ItemType Directory -Force -Path $dir | Out-Null

function Find-Adb {
    $wingetBase = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (Test-Path $wingetBase) {
        Get-ChildItem -Path $wingetBase -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "Google.PlatformTools*" } |
            ForEach-Object {
                $p = Join-Path $_.FullName "platform-tools\adb.exe"
                if (Test-Path -LiteralPath $p) { return $p }
            } | Select-Object -First 1
    }
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

function Ensure-PlatformTools {
    $adb = Find-Adb
    if ($adb) { return $adb }
    Write-Host "Installo Google Platform Tools (adb) con winget..." -ForegroundColor Cyan
    $p = Start-Process winget -ArgumentList @("install", "--id", "Google.PlatformTools", "-e", "--accept-package-agreements", "--accept-source-agreements") -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne $null) {
        Write-Host "winget exit $($p.ExitCode)" -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 2
    $adb = Find-Adb
    if (-not $adb) {
        Write-Host "adb ancora assente. Esegui manualmente: winget install Google.PlatformTools" -ForegroundColor Red
        exit 2
    }
    return $adb
}

if (-not (Test-Path -LiteralPath $apk) -or (Get-Item $apk).Length -lt 10MB) {
    Write-Host "Scarico HTS da GitHub (release ufficiale, ~77 MB)..." -ForegroundColor Cyan
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $apk -UseBasicParsing
}

$h = (Get-FileHash -LiteralPath $apk -Algorithm SHA256).Hash
if ($h -ne $expectedSha256) {
    Write-Host "ERRORE: SHA256 APK non coincide con la release v1.1.0 ufficiale." -ForegroundColor Red
    Write-Host "  Atteso: $expectedSha256" -ForegroundColor Yellow
    Write-Host "  File:   $h" -ForegroundColor Yellow
    Write-Host "Elimina $apk e rilancia lo script." -ForegroundColor Yellow
    exit 4
}
Write-Host "APK verificato (SHA256 OK, fonte GitHub wengmister/hand-tracking-streamer v1.1.0)" -ForegroundColor DarkGreen

$adb = Ensure-PlatformTools
Write-Host "adb: $adb" -ForegroundColor Gray

& $adb kill-server 2>$null
Start-Sleep -Milliseconds 500
& $adb start-server
$lines = & $adb devices
Write-Host ($lines -join "`n")

$txt = $lines -join "`n"
if ($txt -match "unauthorized") {
    Write-Host ""
    Write-Host "=== Quest in stato UNAUTHORIZED ===" -ForegroundColor Red
    Write-Host "1. Indossa il Quest e guarda dentro la cuffia." -ForegroundColor Yellow
    Write-Host "2. Accetta il dialogo 'Consenti debug USB?' / impronta RSA del PC." -ForegroundColor Yellow
    Write-Host "3. Spunta 'Consenti sempre da questo computer' se c'e." -ForegroundColor Yellow
    Write-Host "4. Rilancia: .\scripts\install_hand_tracking_streamer_quest.ps1" -ForegroundColor Cyan
    exit 5
}
if ($txt -notmatch "\tdevice") {
    Write-Host ""
    Write-Host "Nessun dispositivo in stato 'device'. Collega USB, attiva Developer mode, riprova." -ForegroundColor Red
    exit 3
}

Write-Host "Installo sul Quest (adb install -r)..." -ForegroundColor Cyan
& $adb install -r $apk
if ($LASTEXITCODE -ne 0) {
    Write-Host "Install fallita (codice $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host ""
Write-Host "OK. Apri sul Quest: Hand Tracking Streamer (app non ufficiali / Unknown sources)." -ForegroundColor Green
Write-Host "Meta Store (stessa app): https://www.meta.com/experiences/hand-tracking-streamer/26303946202523164" -ForegroundColor Gray
