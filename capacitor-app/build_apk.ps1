# Build APK G1 Talk
# Consigliato: SDK utente (nessun admin). Setup una tantum:
#   ..\scripts\setup_user_android_sdk.ps1
# In alternativa: SDK in Program Files + licenze (spesso serve Amministratore).

$ErrorActionPreference = "Stop"
$jdkPath = "C:\Program Files (x86)\Android\openjdk\jdk-17.0.8.101-hotspot"
$sdkSystem = "C:\Program Files (x86)\Android\android-sdk"
$sdkUser = Join-Path $env:USERPROFILE "AndroidSdkG1"

Write-Host "=== G1 Talk - Build APK ===" -ForegroundColor Cyan

$sdkPath = $sdkSystem
if (Test-Path (Join-Path $sdkUser "platforms\android-32")) {
    $sdkPath = $sdkUser
    Write-Host "Uso SDK utente: $sdkPath" -ForegroundColor Green
} else {
    Write-Host "Uso SDK sistema: $sdkPath (se build fallisce per licenze, esegui ..\scripts\setup_user_android_sdk.ps1)" -ForegroundColor Yellow
    $licDir = "$sdkPath\licenses"
    if (-not (Test-Path "$licDir\android-sdk-license")) {
        Write-Host "Creazione licenze SDK (serve spesso Amministratore per Program Files)..." -ForegroundColor Yellow
        if (-not (Test-Path $licDir)) {
            New-Item -ItemType Directory -Path $licDir -Force | Out-Null
        }
        try {
            Set-Content -Path "$licDir\android-sdk-license" -Value "`n8933bad161af4178b1185d1a37fbf41ea5269c55"
            Set-Content -Path "$licDir\android-sdk-preview-license" -Value "`n84831b9409646a918e30573bab4c9c91346d8abd"
        } catch {
            Write-Host "Impossibile scrivere licenze. Esegui come Admin o setup_user_android_sdk.ps1" -ForegroundColor Red
        }
    }
}

$env:JAVA_HOME = $jdkPath
$env:ANDROID_HOME = $sdkPath

$root = Split-Path -Parent $PSScriptRoot
$androidDir = Join-Path $PSScriptRoot "android"
$localProps = Join-Path $androidDir "local.properties"
$gradleSdk = $sdkPath[0] + '\:\\' + $sdkPath.Substring(3).Replace('\', '\\')
Set-Content -Path $localProps -Value "sdk.dir=$gradleSdk" -Encoding ASCII

Write-Host "Compilazione APK (puo richiedere alcuni minuti)..." -ForegroundColor Yellow
Push-Location $androidDir
try {
    & .\gradlew.bat assembleDebug
    if ($LASTEXITCODE -eq 0) {
        $apk = Get-ChildItem -Path "app\build\outputs\apk\debug\*.apk" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($apk) {
            $destDir = Join-Path $root "dist"
            if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
            Copy-Item $apk.FullName (Join-Path $destDir "G1-Talk-Launcher-debug.apk") -Force
            Copy-Item $apk.FullName (Join-Path $destDir "g1-talk.apk") -Force
            Write-Host ""
            Write-Host "APK: $destDir\G1-Talk-Launcher-debug.apk" -ForegroundColor Green
            Write-Host "Trasferiscilo sul telefono e installalo." -ForegroundColor Cyan
        }
    } else {
        Write-Host "Build fallito." -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
