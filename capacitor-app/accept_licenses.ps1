# Crea licenze Android SDK (richiede esecuzione come amministratore)
$licDir = "C:\Program Files (x86)\Android\android-sdk\licenses"
if (-not (Test-Path $licDir)) {
    New-Item -ItemType Directory -Path $licDir -Force
}
Set-Content -Path "$licDir\android-sdk-license" -Value "`n8933bad161af4178b1185d1a37fbf41ea5269c55"
Set-Content -Path "$licDir\android-sdk-preview-license" -Value "`n84831b9409646a918e30573bab4c9c91346d8abd"
Write-Host "Licenze create in $licDir"
