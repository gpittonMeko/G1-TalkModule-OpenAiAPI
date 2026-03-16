# Avvia l'installer grafico (configurazione chiave API)
# Uso: .\avvia_installer.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Push-Location $root

try {
    if (Test-Path ".venv\Scripts\python.exe") {
        .\.venv\Scripts\python.exe -m installer.main
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m installer.main
    } else {
        Write-Host "Python non trovato. Esegui prima: bash install.sh" -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}
