# Crea SDK Android in %USERPROFILE%\AndroidSdkG1 (nessun admin; evita licenze in Program Files).
# Uso: .\scripts\setup_user_android_sdk.ps1
# Poi: .\capacitor-app\build_apk.ps1

$ErrorActionPreference = "Stop"
$userSdk = Join-Path $env:USERPROFILE "AndroidSdkG1"
$systemSdk = "C:\Program Files (x86)\Android\android-sdk"
$jdk = "C:\Program Files (x86)\Android\openjdk\jdk-17.0.8.101-hotspot"
$sm = Join-Path $userSdk "cmdline-tools\11.0\bin\sdkmanager.bat"

if (-not (Test-Path $sm)) {
    Write-Host "Copia cmdline-tools in $userSdk ..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $userSdk -Force | Out-Null
    robocopy (Join-Path $systemSdk "cmdline-tools") (Join-Path $userSdk "cmdline-tools") /E /NFL /NDL /NJH /NJS /nc /ns /np | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy cmdline-tools fallito" }
}

$env:JAVA_HOME = $jdk
Write-Host "Installazione platform-tools, android-32, build-tools 30.0.3 ..." -ForegroundColor Cyan
$yes = ("y`n" * 80)
$yes | & $sm --sdk_root=$userSdk "platform-tools" "platforms;android-32" "build-tools;30.0.3"

$lp = (Resolve-Path (Join-Path $PSScriptRoot "..\capacitor-app\android\local.properties")).Path
# Gradle: sdk.dir=C\:\\Users\\nome\\AndroidSdkG1
$gradleSdk = $userSdk[0] + '\:\\' + $userSdk.Substring(3).Replace('\', '\\')
Set-Content -Path $lp -Value "sdk.dir=$gradleSdk" -Encoding ASCII

Write-Host "local.properties aggiornato." -ForegroundColor Green
Write-Host "Fatto. Esegui: .\capacitor-app\build_apk.ps1" -ForegroundColor Cyan
