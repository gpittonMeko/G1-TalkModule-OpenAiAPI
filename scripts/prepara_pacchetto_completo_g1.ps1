# Pacchetto completo Unitree G1: server ZIP + soundboard audio + APK (se build ok)
# Uso dalla root:  .\scripts\prepara_pacchetto_completo_g1.ps1
# Opzione: -NoApk   salta la compilazione Gradle

param([switch]$NoApk)

$ErrorActionPreference = "Continue"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dist = Join-Path $root "dist"
$pkgName = "G1_Pacchetto_Installazione_Completa"
$pkgDir = Join-Path $dist $pkgName
$finalZip = Join-Path $dist "$pkgName.zip"

Write-Host "=== Pacchetto installazione G1 (server + audio + APK) ===" -ForegroundColor Cyan

# 1) Zip server Linux
& (Join-Path $PSScriptRoot "crea_pacchetto.ps1")
$serverZip = Join-Path $dist "G1-TalkModule-OpenAiAPI.zip"
if (-not (Test-Path $serverZip)) {
    Write-Host "ERRORE: manca $serverZip" -ForegroundColor Red
    exit 1
}

# 2) Audio soundboard
Push-Location $root
python scripts/export_soundboard_audio.py 2>$null
Pop-Location
$soundDir = Join-Path $dist "soundboard_audio"

# 3) APK (opzionale)
$apkDestName = "G1-Talk-Launcher-debug.apk"
$apkOut = Join-Path $dist $apkDestName
if (-not $NoApk) {
    $jdk = "C:\Program Files (x86)\Android\openjdk\jdk-17.0.8.101-hotspot"
    $sdk = "C:\Program Files (x86)\Android\android-sdk"
    if ((Test-Path $jdk) -and (Test-Path (Join-Path $root "capacitor-app\android\gradlew.bat"))) {
        Write-Host "Tentativo build APK..." -ForegroundColor Yellow
        Push-Location (Join-Path $root "capacitor-app")
        npx cap sync 2>$null
        Pop-Location
        $env:JAVA_HOME = $jdk
        $env:ANDROID_HOME = $sdk
        Push-Location (Join-Path $root "capacitor-app\android")
        & .\gradlew.bat assembleDebug --no-daemon 2>&1 | Out-Null
        $apkOk = $LASTEXITCODE -eq 0
        $built = Get-ChildItem -Path "app\build\outputs\apk\debug\*.apk" -ErrorAction SilentlyContinue | Select-Object -First 1
        Pop-Location
        if ($apkOk -and $built) {
            Copy-Item $built.FullName $apkOut -Force
            Write-Host "APK creato: $apkOut" -ForegroundColor Green
        } else {
            Write-Host "Build APK non riuscita (spesso: accetta licenze SDK come Amministratore). Esegui: capacitor-app\build_apk.ps1" -ForegroundColor Yellow
        }
    } else {
        Write-Host "JDK/Android SDK non trovati: salto APK. Vedi capacitor-app\README.md" -ForegroundColor Yellow
    }
}

# 4) Assembla cartella distribuzione
if (Test-Path $pkgDir) { Remove-Item $pkgDir -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $pkgDir "01_Server_UnitreeG1") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $pkgDir "02_Soundboard_Audio") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $pkgDir "03_APK_Android") -Force | Out-Null

Copy-Item $serverZip (Join-Path $pkgDir "01_Server_UnitreeG1\G1-TalkModule-OpenAiAPI.zip") -Force
if (Test-Path $soundDir) {
    Copy-Item (Join-Path $soundDir "*") (Join-Path $pkgDir "02_Soundboard_Audio") -Recurse -Force
}

$readmeApk = Join-Path $pkgDir "03_APK_Android\LEGGIMI_APK.txt"
@"
G1 Talk - Launcher Android
--------------------------
- Installa l'APK sul telefono (permetti fonti sconosciute se richiesto).
- Connetti il telefono al WiFi del robot (stessa rete del server G1 Talk).
- Avvia il server sul G1 (bash scripts/restart_server.sh) e annota l'IP.
- Apri l'app, inserisci l'IP, Connetti.

Cassa Bluetooth: Impostazioni -> Dispositivi -> associa l'altoparlante; l'audio
dell'app usa l'uscita multimediale (di solito la cassa se selezionata).

Build da sorgente (PC con Android Studio):
  cd capacitor-app
  npm install
  npx cap sync
  npx cap open android
  Build -> Build APK

Oppure da PowerShell (spesso serve Esegui come amministratore per le licenze SDK):
  .\capacitor-app\build_apk.ps1
"@ | Set-Content $readmeApk -Encoding UTF8

if (Test-Path $apkOut) {
    Copy-Item $apkOut (Join-Path $pkgDir "03_APK_Android\$apkDestName") -Force
}

Copy-Item (Join-Path $PSScriptRoot "LEGGIMI_PACCHETTO_COMPLETO.txt") (Join-Path $pkgDir "LEGGIMI_INSTALLAZIONE_COMPLETA.txt") -Force

if (Test-Path $finalZip) { Remove-Item $finalZip -Force }
Compress-Archive -Path $pkgDir -DestinationPath $finalZip -Force

$zSize = [math]::Round((Get-Item $finalZip).Length / 1MB, 2)
Write-Host ""
Write-Host "Pacchetto pronto: $finalZip  ($zSize MB)" -ForegroundColor Green
Write-Host "Cartella: $pkgDir" -ForegroundColor Gray
Write-Host ""
