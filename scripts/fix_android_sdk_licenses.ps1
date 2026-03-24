# Eseguite come Amministratore: aggiunge hash licenze mancanti (Gradle / Platform 32).
# Tasto destro -> Esegui con PowerShell come amministratore
$ErrorActionPreference = "Stop"
$lic = "C:\Program Files (x86)\Android\android-sdk\licenses\android-sdk-license"
$toAdd = @(
    "24333f8a63b6825ea9c5514f83c2829b004d1fee",
    "d56f5187479451eabf01fb78af6dfcb131648e3",
    "84831b9409646a918e30573bab4c9c91346d8abd"
)
$existing = if (Test-Path $lic) { Get-Content $lic -Raw } else { "" }
foreach ($h in $toAdd) {
    if ($existing -notmatch [regex]::Escape($h)) {
        Add-Content -Path $lic -Value $h -Encoding ASCII
        Write-Host "Aggiunto: $h"
    } else {
        Write-Host "Gia presente: $h"
    }
}
Write-Host "Fatto. Riprova: cd capacitor-app\android; .\gradlew.bat assembleDebug"
